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
import sys
import tempfile
import threading
import time
import unittest
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

    def test_dead_pid_claim_is_NOT_reaped_while_device_in_use(self) -> None:
        """A dead claimant whose device is still driven by another process survives.

        This is the incident that motivated the guard: an agent runs
        `zsh -c 'simemu claim ios && xcodebuild ...'`. The wrapper shell is the
        recorded claimant and exits immediately, so the claim reads dead — while
        xcodebuild keeps testing against that UDID for minutes. Reaping here
        frees the device for a sibling claim and produces a double-claim.
        """
        udid = "16D8CDC7-48AB-4AA2-A87F-C117AD696549"
        sessions = {
            "s-32cc55": {
                "status": "active",
                "pid": 2_147_000_000,  # dead
                "sim_id": udid,
            }
        }
        busy = [
            f"/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild test "
            f"-project Vivii.xcodeproj -scheme Vivii -destination id={udid}",
            "/sbin/launchd",
        ]
        self.assertEqual(exclusive.collect_stale_session_ids(sessions, busy), [])

    def test_dead_pid_claim_IS_reaped_when_device_idle(self) -> None:
        """Same dead claimant, but nothing references the UDID → reap as before."""
        udid = "16D8CDC7-48AB-4AA2-A87F-C117AD696549"
        sessions = {
            "s-32cc55": {
                "status": "active",
                "pid": 2_147_000_000,  # dead
                "sim_id": udid,
            }
        }
        idle = ["/sbin/launchd", "/usr/libexec/logd"]
        self.assertEqual(
            exclusive.collect_stale_session_ids(sessions, idle), ["s-32cc55"]
        )

    def test_live_claimant_is_never_reaped_even_when_device_idle(self) -> None:
        """A live claimant is untouched regardless of device activity."""
        sessions = {
            "s-live": {"status": "active", "pid": os.getpid(), "sim_id": "some-udid"}
        }
        self.assertEqual(exclusive.collect_stale_session_ids(sessions, []), [])

    def test_terminal_sessions_are_skipped(self) -> None:
        """Already-expired/released sessions are never re-reaped."""
        sessions = {
            "s-gone": {
                "status": "expired",
                "pid": 2_147_000_000,
                "sim_id": "dead-udid",
            },
            "s-freed": {
                "status": "released",
                "pid": 2_147_000_000,
                "sim_id": "dead-udid",
            },
        }
        self.assertEqual(exclusive.collect_stale_session_ids(sessions, []), [])

    def test_session_without_pid_is_not_reaped_on_pid_grounds(self) -> None:
        """Legacy rows with no pid fall through to heartbeat/expiry logic."""
        sessions = {"s-legacy": {"status": "active", "sim_id": "some-udid"}}
        self.assertEqual(exclusive.collect_stale_session_ids(sessions, []), [])

    def test_device_in_use_matching(self) -> None:
        """device_in_use is a plain substring probe, erring toward 'in use'."""
        udid = "D02DA210-2836-4E4B-B76D-0DB868465103"
        self.assertTrue(exclusive.device_in_use(udid, [f"xcodebuild -destination id={udid}"]))
        self.assertFalse(exclusive.device_in_use(udid, ["xcodebuild -destination id=OTHER"]))
        self.assertFalse(exclusive.device_in_use(udid, []))
        # No sim_id recorded → cannot be in use.
        self.assertFalse(exclusive.device_in_use(None, [f"anything {udid}"]))
        self.assertFalse(exclusive.device_in_use("", [f"anything {udid}"]))

    def test_only_the_idle_device_is_reaped_when_both_claimants_are_dead(self) -> None:
        """Two dead claimants, one busy device: exactly one is reaped."""
        busy_udid = "AAAAAAAA-0000-0000-0000-000000000001"
        idle_udid = "BBBBBBBB-0000-0000-0000-000000000002"
        sessions = {
            "s-busy": {"status": "active", "pid": 2_147_000_000, "sim_id": busy_udid},
            "s-idle": {"status": "active", "pid": 2_147_000_000, "sim_id": idle_udid},
        }
        lines = [f"xcodebuild test -destination id={busy_udid}"]
        self.assertEqual(
            exclusive.collect_stale_session_ids(sessions, lines), ["s-idle"]
        )

    def test_probe_is_not_run_when_there_are_no_dead_claimants(self) -> None:
        """No candidates → the process table is never probed."""
        sessions = {
            "s-live": {"status": "active", "pid": os.getpid(), "sim_id": "some-udid"}
        }
        with patch(
            "simemu.exclusive.running_process_command_lines",
            side_effect=AssertionError("probe must not run"),
        ):
            self.assertEqual(exclusive.collect_stale_session_ids(sessions), [])

    def test_probe_failure_fails_closed_not_reaped(self) -> None:
        """If the ps probe FAILS (returns None) a dead claim is NOT reaped.

        T-LU-054: a probe failure previously collapsed to `[]`, which read as
        "confirmed no process references this device" and reaped it — exactly
        the double-claim this module exists to prevent, just triggered by a
        transient probe error instead of a real idle device.
        """
        sessions = {
            "s-dead": {"status": "active", "pid": 2_147_000_000, "sim_id": "some-udid"}
        }
        with patch("simemu.exclusive.running_process_command_lines", return_value=None):
            self.assertEqual(exclusive.collect_stale_session_ids(sessions), [])

    def test_probe_success_with_no_processes_still_reaps(self) -> None:
        """A real empty probe result (success, nothing running) still reaps.

        Distinguishes the fix from over-correcting: `[]` from a probe that
        genuinely ran is not the same as `None` from a probe that failed.
        """
        sessions = {
            "s-dead": {"status": "active", "pid": 2_147_000_000, "sim_id": "some-udid"}
        }
        with patch("simemu.exclusive.running_process_command_lines", return_value=[]):
            self.assertEqual(exclusive.collect_stale_session_ids(sessions), ["s-dead"])

    def test_explicit_empty_command_lines_still_reaps(self) -> None:
        """An explicitly-passed [] (e.g. from a caller's own successful probe)
        is treated as confirmed-empty, not as a failure signal — only the
        internal probe returning None means failure."""
        sessions = {
            "s-dead": {"status": "active", "pid": 2_147_000_000, "sim_id": "some-udid"}
        }
        self.assertEqual(exclusive.collect_stale_session_ids(sessions, []), ["s-dead"])

    def test_running_process_command_lines_returns_real_lines(self) -> None:
        """The default probe actually reads the process table."""
        lines = exclusive.running_process_command_lines()
        self.assertIsInstance(lines, list)
        self.assertTrue(any("launchd" in line for line in lines))

    def test_running_process_command_lines_none_on_probe_error(self) -> None:
        """A subprocess failure surfaces as None, not an empty list."""
        with patch(
            "subprocess.run",
            side_effect=OSError("no such file or directory: ps"),
        ):
            self.assertIsNone(exclusive.running_process_command_lines())

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


class ExclusiveClaimMultiprocessTests(unittest.TestCase):
    """Cross-process flock contention — 4 child processes, 6 devices."""

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


if __name__ == "__main__":
    unittest.main()
