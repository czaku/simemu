"""Tests for exclusive-claim guarantees in simemu.

The contract we're protecting:

1. Two concurrent `claim()` calls against the same device pool MUST each
   receive a distinct sim_id. No double-claim slip-through.
2. When the pool is exhausted (every device already claimed), claim() fails
   fast unless --wait is set; with --wait it retries and returns once a peer
   releases.
3. A session whose claimant PID is no longer alive is "stale" and gets reaped
   by the next claim attempt, freeing the device for re-use.
4. The introspection module `exclusive` correctly identifies dead vs. live
   PIDs.

These tests deliberately mock at the simctl boundary (`find_best_device`,
`ios.boot`, `android.boot`, …) — the device pool is a list of fake
`SimulatorInfo` objects, and claim contention is exercised through the real
fcntl.flock-protected session store on a temp dir.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

# Temp state dir before importing session (mirrors test_session.py setup).
_tmpdir = tempfile.mkdtemp(prefix="simemu-exclusive-test-")
os.environ["SIMEMU_STATE_DIR"] = _tmpdir
os.environ["SIMEMU_CONFIG_DIR"] = _tmpdir

from simemu import exclusive  # noqa: E402
from simemu.discover import NoSimulatorAvailable, SimulatorInfo  # noqa: E402
from simemu.session import (  # noqa: E402
    ClaimSpec,
    SessionError,
    claim,
    get_active_sessions,
    reap_dead_claims,
    release,
)


def _make_sim(sim_id: str, name: str = None) -> SimulatorInfo:
    return SimulatorInfo(
        sim_id=sim_id,
        platform="ios",
        device_name=name or sim_id,
        booted=True,  # booted=True skips ios.boot for simplicity
        runtime="iOS 26.2",
        real_device=False,
    )


class _DevicePool:
    """Thread-safe pool: pops the first unclaimed sim relative to the live session table.

    Used as the `find_best_device` side_effect during concurrent claim tests.
    Returning a still-unclaimed device for each call simulates what the real
    discover module does — but we drive it from a deterministic list so the
    test asserts behavior rather than discovery heuristics.
    """

    def __init__(self, sims: list[SimulatorInfo]) -> None:
        self._sims = sims
        self._lock = threading.Lock()

    def __call__(self, spec: ClaimSpec) -> SimulatorInfo:
        # The real discover.find_best_device already excludes active session
        # sim_ids; mirror that filter here so the race window we exercise is
        # the same one that exists in production (between selection and the
        # lock-acquire-and-save step).
        with self._lock:
            claimed = {s.sim_id for s in get_active_sessions().values()}
            for sim in self._sims:
                if sim.sim_id not in claimed:
                    return sim
            raise NoSimulatorAvailable("pool exhausted")


class ExclusiveClaimTests(unittest.TestCase):
    """Single-process (thread) concurrency tests against the real flock."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="simemu-excl-")
        self._old_state = os.environ.get("SIMEMU_STATE_DIR")
        self._old_config = os.environ.get("SIMEMU_CONFIG_DIR")
        os.environ["SIMEMU_STATE_DIR"] = self.tmpdir.name
        os.environ["SIMEMU_CONFIG_DIR"] = self.tmpdir.name

    def tearDown(self) -> None:
        for key, prev in (
            ("SIMEMU_STATE_DIR", self._old_state),
            ("SIMEMU_CONFIG_DIR", self._old_config),
        ):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        self.tmpdir.cleanup()

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.state.check_maintenance")
    def test_concurrent_claims_get_distinct_udids(self, mock_maint, mock_boot, mock_win) -> None:
        """N concurrent claim() calls return N distinct sim_ids."""
        pool = _DevicePool([_make_sim(f"sim-{i:02d}") for i in range(8)])
        results: list = []
        errors: list = []

        def worker():
            with patch("simemu.session.find_best_device", side_effect=pool):
                try:
                    sess = claim(ClaimSpec(platform="ios"), wait_seconds=10)
                    results.append(sess.sim_id)
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [], f"unexpected errors: {errors}")
        self.assertEqual(len(results), 5)
        self.assertEqual(
            len(set(results)),
            5,
            f"DOUBLE CLAIM DETECTED — results: {results}",
        )

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.state.check_maintenance")
    def test_pool_exhausted_no_wait_fails_fast(self, mock_maint, mock_boot, mock_win) -> None:
        """When every device is claimed, claim() raises immediately without --wait."""
        pool = _DevicePool([_make_sim("solo-sim")])
        with patch("simemu.session.find_best_device", side_effect=pool):
            first = claim(ClaimSpec(platform="ios"))
            self.assertEqual(first.sim_id, "solo-sim")
            with self.assertRaises((NoSimulatorAvailable, SessionError)):
                claim(ClaimSpec(platform="ios"))  # wait_seconds=0

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.erase")
    @patch("simemu.session.ios.shutdown")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.state.check_maintenance")
    def test_wait_releases_when_peer_frees(
        self, mock_maint, mock_boot, mock_shutdown, mock_erase, mock_win
    ) -> None:
        """--wait causes claim() to block until a peer releases the device."""
        pool = _DevicePool([_make_sim("only-sim")])
        with patch("simemu.session.find_best_device", side_effect=pool):
            first = claim(ClaimSpec(platform="ios"))

            def releaser():
                time.sleep(0.5)
                release(first.session_id)

            t = threading.Thread(target=releaser)
            t.start()
            try:
                second = claim(ClaimSpec(platform="ios"), wait_seconds=10)
            finally:
                t.join()
            self.assertEqual(second.sim_id, "only-sim")
            self.assertNotEqual(second.session_id, first.session_id)

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.state.check_maintenance")
    def test_dead_pid_claims_are_reaped(self, mock_maint, mock_boot, mock_win) -> None:
        """A claim whose owner PID died is reaped on next claim attempt."""
        pool = _DevicePool([_make_sim("haunted-sim")])
        with patch("simemu.session.find_best_device", side_effect=pool):
            first = claim(ClaimSpec(platform="ios"))

        # Forge a dead PID onto the persisted session.
        sf = Path(self.tmpdir.name) / "sessions.json"
        data = json.loads(sf.read_text())
        data["sessions"][first.session_id]["pid"] = 1  # init — alive, won't be reaped
        sf.write_text(json.dumps(data))
        self.assertEqual(reap_dead_claims(), [])

        data["sessions"][first.session_id]["pid"] = 2_147_000_000  # ~unused PID
        sf.write_text(json.dumps(data))
        reaped = reap_dead_claims()
        self.assertIn(first.session_id, reaped)

        # Re-claim should now succeed against the same device.
        with patch("simemu.session.find_best_device", side_effect=pool):
            second = claim(ClaimSpec(platform="ios"))
        self.assertEqual(second.sim_id, "haunted-sim")
        self.assertNotEqual(second.session_id, first.session_id)

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_survives_multi_minute_gap_with_live_pid(self, mock_maint, mock_boot, mock_win) -> None:
        """T-054: a live holder keeps its claim no matter how long since its
        last call — the exact 'long xcodebuild with no simemu interaction'
        situation a claim exists to protect. Only PID death should ever
        trigger a reap; idle time alone must not.
        """
        pool = _DevicePool([_make_sim("build-sim")])
        with patch("simemu.session.find_best_device", side_effect=pool):
            first = claim(ClaimSpec(platform="ios"))

        sf = Path(self.tmpdir.name) / "sessions.json"
        data = json.loads(sf.read_text())
        # Bind to OUR OWN pid — genuinely alive for the life of this test —
        # and backdate heartbeat_at by several minutes with no `do` calls in
        # between, simulating a long build the agent never touched simemu
        # during.
        data["sessions"][first.session_id]["pid"] = os.getpid()
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        data["sessions"][first.session_id]["heartbeat_at"] = stale
        sf.write_text(json.dumps(data))

        self.assertEqual(reap_dead_claims(), [], "a live-PID claim must never be reaped by idle time alone")
        self.assertIn(first.session_id, get_active_sessions())

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_held_by_killed_process_is_reapable_immediately(self, mock_maint, mock_boot, mock_win) -> None:
        """T-054/T-LU-054: once the real holder process is confirmed dead,
        the claim is reapable on the very next check — no grace period, no
        waiting for an unrelated future claim() to happen to notice."""
        pool = _DevicePool([_make_sim("victim-sim")])
        with patch("simemu.session.find_best_device", side_effect=pool):
            first = claim(ClaimSpec(platform="ios"))

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            sf = Path(self.tmpdir.name) / "sessions.json"
            data = json.loads(sf.read_text())
            data["sessions"][first.session_id]["pid"] = child.pid
            sf.write_text(json.dumps(data))

            # Alive: not reaped.
            self.assertEqual(reap_dead_claims(), [])

            # Kill it and wait for the kernel to actually reap the zombie —
            # is_pid_alive() must see it as gone right away, no polling loop.
            child.kill()
            child.wait(timeout=10)
            reaped = reap_dead_claims()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

        self.assertIn(first.session_id, reaped)
        self.assertNotIn(first.session_id, get_active_sessions())

        # The device is immediately claimable again — no unrelated call needed.
        with patch("simemu.session.find_best_device", side_effect=pool):
            second = claim(ClaimSpec(platform="ios"))
        self.assertEqual(second.sim_id, "victim-sim")

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.erase")
    @patch("simemu.session.ios.shutdown")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.state.check_maintenance")
    def test_release_frees_device_without_needing_reap_call(
        self, mock_maint, mock_boot, mock_shutdown, mock_erase, mock_win
    ) -> None:
        """T-056: an explicit release must free the device immediately —
        visible to `simemu claims`/`sessions` right away, with no dependency
        on the PID-liveness reaper or another unrelated claim() call."""
        pool = _DevicePool([_make_sim("giveback-sim")])
        with patch("simemu.session.find_best_device", side_effect=pool):
            first = claim(ClaimSpec(platform="ios"))
        self.assertIn(first.session_id, get_active_sessions())

        released = release(first.session_id)
        self.assertEqual(released.status, "released")
        self.assertNotIn(first.session_id, get_active_sessions())

        with patch("simemu.session.find_best_device", side_effect=pool):
            second = claim(ClaimSpec(platform="ios"))
        self.assertEqual(second.sim_id, "giveback-sim")
        self.assertNotEqual(second.session_id, first.session_id)

    def test_claim_token_round_trip(self) -> None:
        """to_agent_json exposes the token; validate_token() round-trips."""
        token = exclusive.issue_claim_token()
        self.assertEqual(len(token), 32)
        self.assertTrue(exclusive.validate_token(token, token))
        self.assertFalse(exclusive.validate_token(token, token + "x"))
        self.assertFalse(exclusive.validate_token(None, token))
        self.assertFalse(exclusive.validate_token(token, None))


# ─── multiprocessing test ───────────────────────────────────────────────────
#
# This is the harder test — spawn N subprocess workers that each call
# session.claim() against the SAME shared state dir, with simctl boundaries
# mocked inside each child. Validates the real cross-process flock works.

def _child_claim(state_dir: str, sim_ids: list[str], result_q) -> None:
    """Subprocess entry: install mocks, call claim(), push sim_id to queue."""
    os.environ["SIMEMU_STATE_DIR"] = state_dir
    os.environ["SIMEMU_CONFIG_DIR"] = state_dir
    # Re-import so the env vars apply to this child's state-dir resolution.
    from simemu.discover import SimulatorInfo as _Sim
    from simemu import session as _sess

    sims = [
        _Sim(sim_id=s, platform="ios", device_name=s, booted=True,
             runtime="iOS 26.2", real_device=False)
        for s in sim_ids
    ]

    def _pick(spec):
        from simemu.session import get_active_sessions as _gas
        claimed = {s.sim_id for s in _gas().values()}
        for s in sims:
            if s.sim_id not in claimed:
                return s
        from simemu.discover import NoSimulatorAvailable as _Nope
        raise _Nope("pool exhausted")

    with patch.object(_sess, "find_best_device", side_effect=_pick), \
         patch.object(_sess.ios, "boot"), \
         patch.object(_sess.window_mgr, "apply_window_mode"), \
         patch.object(_sess.state, "check_maintenance"):
        try:
            s = _sess.claim(_sess.ClaimSpec(platform="ios"), wait_seconds=15)
            result_q.put(("ok", s.sim_id, s.session_id, os.getpid()))
        except Exception as exc:
            result_q.put(("err", repr(exc), "", os.getpid()))


def _child_claim_no_wait(state_dir: str, sim_id: str, result_q) -> None:
    """Subprocess entry: N processes race for ONE device, no --wait.

    Exactly one must win; everyone else must fail fast and cleanly rather
    than hang, crash, or silently succeed against the same device.
    """
    os.environ["SIMEMU_STATE_DIR"] = state_dir
    os.environ["SIMEMU_CONFIG_DIR"] = state_dir
    from simemu.discover import SimulatorInfo as _Sim
    from simemu import session as _sess

    sim = _Sim(sim_id=sim_id, platform="ios", device_name=sim_id, booted=True,
               runtime="iOS 26.2", real_device=False)

    def _pick(spec):
        from simemu.session import get_active_sessions as _gas
        if any(s.sim_id == sim_id for s in _gas().values()):
            from simemu.discover import NoSimulatorAvailable as _Nope
            raise _Nope("pool exhausted")
        return sim

    with patch.object(_sess, "find_best_device", side_effect=_pick), \
         patch.object(_sess.ios, "boot"), \
         patch.object(_sess.window_mgr, "apply_window_mode"), \
         patch.object(_sess.state, "check_maintenance"):
        try:
            s = _sess.claim(_sess.ClaimSpec(platform="ios"), wait_seconds=0)
            result_q.put(("ok", s.sim_id, s.session_id, os.getpid()))
        except Exception as exc:
            error_type = getattr(exc, "error_type", type(exc).__name__)
            result_q.put(("err", error_type, "", os.getpid()))


class ExclusiveClaimMultiprocessTests(unittest.TestCase):
    """Cross-process flock contention — 4 child processes, 6 devices."""

    def test_concurrent_subprocess_claims_on_single_device_only_one_wins(self) -> None:
        """T-054/T-LU-054 verification: a second claimant cannot act on a
        device while the first holds it, and gets a clear error — proven
        under REAL cross-process concurrency, not sequential mocked calls."""
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory(prefix="simemu-excl-mp-single-") as td:
            q = ctx.Queue()
            procs = [
                ctx.Process(target=_child_claim_no_wait, args=(td, "solo-mp-sim", q))
                for _ in range(5)
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=60)
                self.assertEqual(p.exitcode, 0, f"child exited {p.exitcode}")

            results = []
            while not q.empty():
                results.append(q.get_nowait())

            oks = [r for r in results if r[0] == "ok"]
            errs = [r for r in results if r[0] == "err"]
            self.assertEqual(len(results), 5, f"expected 5 results, got {len(results)}: {results}")
            self.assertEqual(len(oks), 1, f"expected exactly 1 winner, got {len(oks)}: {results}")
            self.assertEqual(len(errs), 4, f"expected 4 clear losers, got {len(errs)}: {results}")
            for _, error_type, _, _ in errs:
                self.assertIn(
                    error_type, ("device_already_claimed", "NoSimulatorAvailable"),
                    f"unclear/unexpected error on the losing side: {error_type}",
                )

    def test_concurrent_subprocess_claims_get_distinct_udids(self) -> None:
        # Use 'spawn' so children don't inherit the unittest temp-dir env.
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory(prefix="simemu-excl-mp-") as td:
            sim_ids = [f"mp-sim-{i:02d}" for i in range(6)]
            q = ctx.Queue()
            procs = [
                ctx.Process(target=_child_claim, args=(td, sim_ids, q))
                for _ in range(4)
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=60)
                self.assertEqual(p.exitcode, 0, f"child exited {p.exitcode}")

            results = []
            while not q.empty():
                results.append(q.get_nowait())

            oks = [r for r in results if r[0] == "ok"]
            errs = [r for r in results if r[0] == "err"]
            self.assertEqual(errs, [], f"subprocess errors: {errs}")
            self.assertEqual(len(oks), 4, f"expected 4 oks, got {len(oks)}: {results}")
            sim_ids_claimed = [r[1] for r in oks]
            session_ids = [r[2] for r in oks]
            self.assertEqual(
                len(set(sim_ids_claimed)),
                4,
                f"DOUBLE CLAIM across processes: {sim_ids_claimed}",
            )
            self.assertEqual(len(set(session_ids)), 4)


class PpidAndCommandRetryTests(unittest.TestCase):
    """Unit tests for exclusive._ppid_and_command's bounded retry.

    Gate finding (round 2): a `ps` probe failure was treated identically to a
    confirmed "no such process," even though a spawn/timeout failure is
    plausibly transient (a momentarily overloaded box -- the exact "long
    xcodebuild eating CPU" scenario this subsystem exists to tolerate) while
    a clean non-zero exit is `ps` authoritatively reporting the pid is gone.
    """

    def test_retries_on_transient_subprocess_failure_then_succeeds(self) -> None:
        calls = {"n": 0}

        def fake_run(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("transient")
            return subprocess.CompletedProcess(args, 0, stdout="123 /usr/bin/durable\n", stderr="")

        with patch("simemu.exclusive.subprocess.run", side_effect=fake_run), \
             patch("simemu.exclusive.time.sleep"):
            parent, command = exclusive._ppid_and_command(999)
        self.assertEqual(parent, 123)
        self.assertEqual(command, "/usr/bin/durable")
        self.assertEqual(calls["n"], 3)

    def test_gives_up_after_bounded_retries(self) -> None:
        with patch("simemu.exclusive.subprocess.run", side_effect=OSError("gone")), \
             patch("simemu.exclusive.time.sleep") as mock_sleep:
            parent, command = exclusive._ppid_and_command(999)
        self.assertEqual((parent, command), (None, None))
        self.assertEqual(mock_sleep.call_count, exclusive._PS_RETRY_ATTEMPTS - 1)

    def test_does_not_retry_a_clean_no_such_process_result(self) -> None:
        """A non-zero exit with `ps` running fine is authoritative -- the pid
        is confirmed gone, not a hiccup. Retrying wouldn't change that."""
        calls = {"n": 0}

        def fake_run(*args, **kwargs):
            calls["n"] += 1
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

        with patch("simemu.exclusive.subprocess.run", side_effect=fake_run), \
             patch("simemu.exclusive.time.sleep") as mock_sleep:
            parent, command = exclusive._ppid_and_command(999)
        self.assertEqual((parent, command), (None, None))
        self.assertEqual(calls["n"], 1)
        mock_sleep.assert_not_called()


class OneShotShellDetectionTests(unittest.TestCase):
    """Unit tests for exclusive._looks_like_one_shot_shell."""

    def test_zsh_dash_c_is_one_shot(self) -> None:
        self.assertTrue(exclusive._looks_like_one_shot_shell("/bin/zsh -c echo hi"))

    def test_bash_dash_c_is_one_shot(self) -> None:
        self.assertTrue(exclusive._looks_like_one_shot_shell("bash -c 'echo hi'"))

    def test_combined_login_command_flag_is_one_shot(self) -> None:
        self.assertTrue(exclusive._looks_like_one_shot_shell("/bin/zsh -lc 'echo hi'"))

    def test_sh_dash_c_is_one_shot(self) -> None:
        self.assertTrue(exclusive._looks_like_one_shot_shell("/bin/sh -c 'echo hi'"))

    def test_interactive_shell_is_not_one_shot(self) -> None:
        self.assertFalse(exclusive._looks_like_one_shot_shell("-zsh"))
        self.assertFalse(exclusive._looks_like_one_shot_shell("/bin/zsh"))

    def test_non_shell_command_is_not_one_shot(self) -> None:
        self.assertFalse(exclusive._looks_like_one_shot_shell(
            "/Users/luke/.local/bin/claude --dangerously-skip-permissions"
        ))

    def test_empty_command_is_not_one_shot(self) -> None:
        self.assertFalse(exclusive._looks_like_one_shot_shell(""))

    def test_script_with_c_argument_is_not_one_shot(self) -> None:
        """A script's OWN '-c' argument must not be mistaken for the shell's
        own -c flag: 'bash build.sh -c' is a durable script run (build.sh is
        the first positional argument), not a one-shot -c invocation."""
        self.assertFalse(exclusive._looks_like_one_shot_shell("bash build.sh -c"))

    def test_dash_o_option_operand_is_not_mistaken_for_positional(self) -> None:
        """'-o errexit' sets a shell option and takes an operand that is NOT
        itself a flag -- it must be skipped, not mistaken for the first
        positional argument, so a real -c later on is still detected."""
        self.assertTrue(exclusive._looks_like_one_shot_shell("bash -o errexit -c cmd"))
        self.assertTrue(exclusive._looks_like_one_shot_shell("bash -o errexit -o nounset -c cmd"))
        self.assertFalse(exclusive._looks_like_one_shot_shell("bash -o"))

    def test_plus_o_option_operand_is_not_mistaken_for_positional(self) -> None:
        """'+o' (unset a shell option) is the '-o' toggle's counterpart and
        must be recognized the same way -- it does NOT start with '-', so a
        naive positional-argument check would misread it as one."""
        self.assertTrue(exclusive._looks_like_one_shot_shell("bash +o errexit -c cmd"))
        self.assertFalse(exclusive._looks_like_one_shot_shell("bash +o"))

    def test_end_of_options_marker_stops_flag_scanning(self) -> None:
        """A bare '--' is the POSIX end-of-options marker: bash treats a
        literal '-c' after it as a positional filename, not the -c flag."""
        self.assertFalse(exclusive._looks_like_one_shot_shell("bash -- -c"))


class DurableAncestorTests(unittest.TestCase):
    """Unit tests for exclusive._find_durable_ancestor and claimant_pid().

    T-LU-054's root-cause finding: each simemu invocation is typically run by
    a fresh `<shell> -c "simemu ..."` wrapper that exits the instant that one
    command returns, so os.getppid() is a near-useless liveness anchor — it's
    already gone before the agent's next call. These tests exercise the
    ancestor walk that skips past that wrapper to find the process that
    actually persists across calls.
    """

    def test_walks_past_single_one_shot_wrapper(self) -> None:
        # pid 100 (immediate parent) is a one-shot wrapper whose own parent,
        # pid 50, is the durable harness process.
        table = {
            100: (50, "/bin/zsh -c simemu claim ios"),
            50: (5, "/usr/bin/claude"),
        }
        with patch("simemu.exclusive._ppid_and_command",
                    side_effect=lambda pid: table.get(pid, (None, None))):
            self.assertEqual(exclusive._find_durable_ancestor(100), 50)

    def test_walks_past_chained_one_shot_wrappers(self) -> None:
        # 300 -c-> 200 -c-> 100, then 100's parent (10) is durable.
        table = {
            300: (200, "/bin/zsh -c inner"),
            200: (100, "/bin/bash -c middle"),
            100: (10, "/bin/sh -c outer"),
            10: (1, "/usr/bin/sweech"),
        }
        with patch("simemu.exclusive._ppid_and_command",
                    side_effect=lambda pid: table.get(pid, (None, None))):
            self.assertEqual(exclusive._find_durable_ancestor(300), 10)

    def test_stops_immediately_for_non_wrapper_parent(self) -> None:
        """Interactive terminal / already-durable parent: unchanged behavior."""
        with patch("simemu.exclusive._ppid_and_command", return_value=(1, "-zsh")):
            self.assertEqual(exclusive._find_durable_ancestor(777), 777)

    def test_falls_back_to_start_pid_when_ps_unavailable(self) -> None:
        """`ps` failing on the FIRST probe must reproduce the pre-existing
        (already-shipped) behavior exactly — never worse, never a crash."""
        with patch("simemu.exclusive._ppid_and_command", return_value=(None, None)):
            self.assertEqual(exclusive._find_durable_ancestor(4242), 4242)

    def test_falls_back_to_start_pid_when_probe_fails_mid_walk(self) -> None:
        """Gate finding on the original implementation: a probe failure
        PARTWAY through the walk (not on the first hop) must not be silently
        trusted as a durable ancestor. pid 100 is a confirmed one-shot
        wrapper whose parent is 50, but 50's own probe fails (e.g. it exited
        in the gap between reading its ppid and reading its command) — the
        walk must fall all the way back to start_pid (100), never return the
        unverified 50, since an unverified pid could itself be transient and
        reproduce the exact premature-reap bug this walk exists to prevent.
        """
        table = {100: (50, "/bin/zsh -c simemu claim ios")}
        with patch("simemu.exclusive._ppid_and_command",
                    side_effect=lambda pid: table.get(pid, (None, None))):
            self.assertEqual(exclusive._find_durable_ancestor(100), 100)

    def test_bounded_hop_count_never_loops_forever(self) -> None:
        # A pathological chain longer than _MAX_ANCESTOR_HOPS must still
        # terminate. Every hop along the way is a CONFIRMED one-shot wrapper
        # (never an unverified probe failure), so hitting the hop limit
        # without ever confirming a durable ancestor falls back to start_pid
        # rather than trusting the last (unverified-as-durable) hop reached.
        table = {pid: (pid + 1, f"/bin/zsh -c step{pid}") for pid in range(1, 30)}
        with patch("simemu.exclusive._ppid_and_command",
                    side_effect=lambda pid: table.get(pid, (None, None))):
            result = exclusive._find_durable_ancestor(1)
        self.assertEqual(result, 1)

    def test_claimant_pid_env_override_wins_over_ancestor_walk(self) -> None:
        with patch.dict(os.environ, {"SIMEMU_CLAIMANT_PID": "555"}):
            self.assertEqual(exclusive.claimant_pid(), 555)

    def test_claimant_pid_uses_durable_ancestor_of_getppid(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SIMEMU_CLAIMANT_PID", None)
            with patch("os.getppid", return_value=100), \
                 patch("simemu.exclusive._find_durable_ancestor", return_value=50) as mock_walk:
                self.assertEqual(exclusive.claimant_pid(), 50)
            mock_walk.assert_called_once_with(100)


if __name__ == "__main__":
    unittest.main()
