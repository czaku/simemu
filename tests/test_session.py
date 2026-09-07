"""Tests for simemu.session — session lifecycle, claim, touch, renew, release."""

import io
import json
import os
import tempfile
import time
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, call, patch

# Set up temp state dir before importing session module
_tmpdir = tempfile.mkdtemp(prefix="simemu-session-test-")
os.environ["SIMEMU_STATE_DIR"] = _tmpdir
os.environ["SIMEMU_CONFIG_DIR"] = _tmpdir

from simemu.discover import SimulatorInfo
from simemu.session import (
    ClaimSpec,
    Session,
    SessionError,
    _compute_expires_at,
    _gen_session_id,
    _now_iso,
    _park_session,
    _read_sessions_raw,
    claim,
    do_command,
    get_active_sessions,
    get_session,
    lifecycle_tick,
    release,
    renew,
    require_session,
    touch,
    IDLE_TIMEOUT,
    PARK_TIMEOUT,
    EXPIRE_TIMEOUT,
    ANDROID_DISCONNECT_GRACE,
    DEFAULT_MEMORY_BUDGET_MB,
    _DEVICE_MEMORY_MB,
)


def _make_sim(
    sim_id: str = "AAA-111",
    platform: str = "ios",
    device_name: str = "iPhone 16 Pro",
    booted: bool = False,
    runtime: str = "iOS 26.2",
    real_device: bool = False,
) -> SimulatorInfo:
    return SimulatorInfo(
        sim_id=sim_id,
        platform=platform,
        device_name=device_name,
        booted=booted,
        runtime=runtime,
        real_device=real_device,
    )


class TestGenSessionId(unittest.TestCase):
    def test_gen_session_id(self) -> None:
        sid = _gen_session_id()
        self.assertTrue(sid.startswith("s-"))
        self.assertEqual(len(sid), 8)  # "s-" + 6 hex chars

    def test_gen_session_id_unique(self) -> None:
        ids = {_gen_session_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)


class TestClaimSpec(unittest.TestCase):
    def test_to_claim_command(self) -> None:
        spec = ClaimSpec(platform="ios")
        cmd = spec.to_claim_command()
        self.assertEqual(cmd, "simemu claim ios")

    def test_to_claim_command_with_all_options(self) -> None:
        spec = ClaimSpec(
            platform="android",
            form_factor="tablet",
            os_version="14",
            real_device=True,
            label="my-test",
        )
        cmd = spec.to_claim_command()
        self.assertIn("simemu claim android", cmd)
        self.assertIn("--version 14", cmd)
        self.assertIn("--form-factor tablet", cmd)
        self.assertIn("--real", cmd)
        self.assertIn("--label 'my-test'", cmd)

    def test_to_claim_command_with_visible(self) -> None:
        spec = ClaimSpec(platform="ios", visible=True)
        cmd = spec.to_claim_command()
        self.assertIn("--show", cmd)


class TestSession(unittest.TestCase):
    def _make_session(self, **overrides) -> Session:
        now = _now_iso()
        defaults = {
            "session_id": "s-abc123",
            "platform": "ios",
            "form_factor": "phone",
            "os_version": None,
            "real_device": False,
            "label": "test-label",
            "status": "active",
            "sim_id": "AAA-111",
            "device_name": "iPhone 16 Pro",
            "agent": "test-agent",
            "created_at": now,
            "heartbeat_at": now,
            "resolved_os_version": "iOS 26.2",
            "claim_platform": "ios",
            "claim_form_factor": "phone",
        }
        defaults.update(overrides)
        return Session(**defaults)

    def test_to_agent_json(self) -> None:
        session = self._make_session()
        j = session.to_agent_json()
        self.assertEqual(j["session"], "s-abc123")
        self.assertEqual(j["platform"], "ios")
        self.assertEqual(j["form_factor"], "phone")
        self.assertEqual(j["status"], "active")
        self.assertEqual(j["label"], "test-label")
        # Must NOT contain internal fields
        self.assertNotIn("sim_id", j)
        self.assertNotIn("device_name", j)
        self.assertNotIn("agent", j)
        self.assertNotIn("heartbeat_at", j)

    def test_to_agent_json_os_version_fallback(self) -> None:
        session = self._make_session(resolved_os_version=None, os_version=None)
        j = session.to_agent_json()
        self.assertEqual(j["os_version"], "latest")

    def test_reclaim_command(self) -> None:
        session = self._make_session(claim_platform="ios", claim_form_factor="phone")
        cmd = session.reclaim_command()
        self.assertIn("simemu claim ios", cmd)


class TestComputeExpiresAt(unittest.TestCase):
    def test_active(self) -> None:
        now = _now_iso()
        expires = _compute_expires_at("active", now)
        hb = datetime.fromisoformat(now)
        exp = datetime.fromisoformat(expires)
        diff = (exp - hb).total_seconds()
        self.assertAlmostEqual(diff, IDLE_TIMEOUT, delta=1)

    def test_idle(self) -> None:
        now = _now_iso()
        expires = _compute_expires_at("idle", now)
        hb = datetime.fromisoformat(now)
        exp = datetime.fromisoformat(expires)
        diff = (exp - hb).total_seconds()
        self.assertAlmostEqual(diff, IDLE_TIMEOUT + PARK_TIMEOUT, delta=1)

    def test_parked(self) -> None:
        now = _now_iso()
        expires = _compute_expires_at("parked", now)
        hb = datetime.fromisoformat(now)
        exp = datetime.fromisoformat(expires)
        diff = (exp - hb).total_seconds()
        self.assertAlmostEqual(diff, EXPIRE_TIMEOUT, delta=1)

    def test_unknown_status_returns_heartbeat(self) -> None:
        now = _now_iso()
        expires = _compute_expires_at("released", now)
        self.assertEqual(expires, now)


class TestClaim(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="simemu-claim-test-")
        self._old_state = os.environ.get("SIMEMU_STATE_DIR")
        self._old_config = os.environ.get("SIMEMU_CONFIG_DIR")
        os.environ["SIMEMU_STATE_DIR"] = self.tmpdir.name
        os.environ["SIMEMU_CONFIG_DIR"] = self.tmpdir.name

    def tearDown(self) -> None:
        if self._old_state is None:
            os.environ.pop("SIMEMU_STATE_DIR", None)
        else:
            os.environ["SIMEMU_STATE_DIR"] = self._old_state
        if self._old_config is None:
            os.environ.pop("SIMEMU_CONFIG_DIR", None)
        else:
            os.environ["SIMEMU_CONFIG_DIR"] = self._old_config
        self.tmpdir.cleanup()

    def _seed_session(self, session_id: str, **overrides) -> None:
        now = _now_iso()
        session_data = {
            "session_id": session_id,
            "platform": "android",
            "form_factor": "phone",
            "os_version": None,
            "real_device": False,
            "label": "",
            "status": "active",
            "sim_id": "Pixel_API34",
            "device_name": "Pixel API34",
            "agent": "test",
            "created_at": now,
            "heartbeat_at": now,
            "expires_at": _compute_expires_at("active", now),
            "resolved_os_version": "API 34",
            "claim_platform": "android",
            "claim_form_factor": "phone",
            "claim_os_version": None,
            "claim_real_device": False,
            "claim_label": "",
            "pinned_serial": "emulator-5554",
        }
        session_data.update(overrides)
        sf = Path(self.tmpdir.name) / "sessions.json"
        if sf.exists():
            data = json.loads(sf.read_text())
        else:
            data = {"sessions": {}}
        data["sessions"][session_id] = session_data
        sf.write_text(json.dumps(data))

    @patch("simemu.session.android.get_android_serial", return_value=None)
    @patch("simemu.session.android.validate_serial", return_value=False)
    def test_get_active_sessions_expires_stale_android_emulators(
        self,
        mock_validate,
        mock_get_serial,
    ) -> None:
        stale_heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=ANDROID_DISCONNECT_GRACE + 5)).isoformat()
        self._seed_session(
            "s-stale",
            sim_id="Stale_API35",
            pinned_serial="emulator-5554",
            heartbeat_at=stale_heartbeat,
            expires_at=_compute_expires_at("active", stale_heartbeat),
        )

        active = get_active_sessions()

        self.assertNotIn("s-stale", active)
        persisted = get_session("s-stale")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, "expired")
        self.assertIsNone(persisted.pinned_serial)

    @patch("simemu.session.android.get_android_serial", return_value=None)
    @patch("simemu.session.android.validate_serial", return_value=False)
    def test_get_active_sessions_keeps_fresh_android_session_during_adb_gap(
        self,
        mock_validate,
        mock_get_serial,
    ) -> None:
        self._seed_session("s-fresh", sim_id="Fresh_API35", pinned_serial="emulator-5554")

        active = get_active_sessions()

        self.assertIn("s-fresh", active)
        persisted = get_session("s-fresh")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, "active")
        self.assertEqual(persisted.pinned_serial, "emulator-5554")

    @patch("simemu.session.android.shutdown")
    def test_park_session_logs_to_stderr(self, mock_shutdown) -> None:
        now = _now_iso()
        session = Session(
            session_id="s-park01",
            platform="android",
            form_factor="phone",
            os_version=None,
            real_device=False,
            label="",
            status="active",
            sim_id="Pixel_API35",
            device_name="Pixel API35",
            agent="test",
            created_at=now,
            heartbeat_at=now,
            expires_at=_compute_expires_at("active", now),
            claim_platform="android",
            claim_form_factor="phone",
        )
        self._seed_session("s-park01", sim_id="Pixel_API35")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            _park_session("s-park01", session)

        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Parked 's-park01' to free memory", stderr.getvalue())

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.find_best_device")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_creates_session(self, mock_maint, mock_find, mock_boot, mock_win) -> None:
        mock_find.return_value = _make_sim(booted=False)
        spec = ClaimSpec(platform="ios")
        session = claim(spec)

        self.assertTrue(session.session_id.startswith("s-"))
        self.assertEqual(session.platform, "ios")
        self.assertEqual(session.status, "active")
        self.assertEqual(session.sim_id, "AAA-111")
        self.assertEqual(session.device_name, "iPhone 16 Pro")

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.find_best_device")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_boots_device(self, mock_maint, mock_find, mock_boot, mock_win) -> None:
        mock_find.return_value = _make_sim(booted=False)
        spec = ClaimSpec(platform="ios")
        claim(spec)
        mock_boot.assert_called_once_with("AAA-111")

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.find_best_device")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_skips_boot_for_booted_device(self, mock_maint, mock_find, mock_boot, mock_win) -> None:
        mock_find.return_value = _make_sim(booted=True)
        spec = ClaimSpec(platform="ios")
        claim(spec)
        mock_boot.assert_not_called()

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.find_best_device")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_skips_boot_for_real_device(self, mock_maint, mock_find, mock_boot, mock_win) -> None:
        mock_find.return_value = _make_sim(booted=False, real_device=True)
        spec = ClaimSpec(platform="ios")
        claim(spec)
        mock_boot.assert_not_called()

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.android.wait_until_ready", return_value="emulator-5554")
    @patch("simemu.session.android.boot")
    @patch("simemu.session.find_best_device")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_boots_android_headless(self, mock_maint, mock_find, mock_boot, mock_ready, mock_win) -> None:
        mock_find.return_value = _make_sim(
            sim_id="Pixel_7", platform="android", device_name="Pixel 7",
            booted=False, runtime="API 35",
        )
        spec = ClaimSpec(platform="android")
        claim(spec)
        mock_boot.assert_called_once_with("Pixel_7", headless=True)

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.android.wait_until_ready", return_value="emulator-5554")
    @patch("simemu.session.android.boot")
    @patch("simemu.session.find_best_device")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_boots_android_windowed_when_visible(self, mock_maint, mock_find, mock_boot, mock_ready, mock_win) -> None:
        mock_find.return_value = _make_sim(
            sim_id="Pixel_7", platform="android", device_name="Pixel 7",
            booted=False, runtime="API 35",
        )
        spec = ClaimSpec(platform="android", visible=True)
        claim(spec)
        mock_boot.assert_called_once_with("Pixel_7", headless=False)
        mock_win.assert_not_called()

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.android.wait_until_ready", return_value="emulator-5556")
    @patch("simemu.session.android.get_android_serial", return_value="emulator-5556")
    @patch("simemu.session.android.boot")
    @patch("simemu.session.find_matching_devices")
    @patch("simemu.session.find_best_device")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_falls_back_when_first_android_emulator_fails_to_boot(
        self,
        mock_maint,
        mock_find,
        mock_find_matching,
        mock_boot,
        mock_get_serial,
        mock_ready,
        mock_win,
    ) -> None:
        bad = _make_sim(
            sim_id="Biscuit_API35", platform="android", device_name="Biscuit API35",
            booted=False, runtime="API 35",
        )
        good = _make_sim(
            sim_id="Pixel8_API34", platform="android", device_name="Pixel8 API34",
            booted=False, runtime="API 34",
        )
        mock_find.return_value = bad
        mock_find_matching.return_value = [bad, good]
        mock_boot.side_effect = [RuntimeError("snapshot pending"), None]

        session = claim(ClaimSpec(platform="android"))

        self.assertEqual(session.sim_id, "Pixel8_API34")
        self.assertEqual(mock_boot.call_args_list[0], call("Biscuit_API35", headless=True))
        self.assertEqual(mock_boot.call_args_list[1], call("Pixel8_API34", headless=True))

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.android.wait_until_ready", return_value="emulator-5556")
    @patch("simemu.session.android.get_android_serial", return_value="emulator-5556")
    @patch("simemu.session.android.boot")
    @patch("simemu.session.find_matching_devices")
    @patch("simemu.session.find_best_device")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_visible_android_fallback_stays_windowed(
        self,
        mock_maint,
        mock_find,
        mock_find_matching,
        mock_boot,
        mock_get_serial,
        mock_ready,
        mock_win,
    ) -> None:
        bad = _make_sim(
            sim_id="Biscuit_API35", platform="android", device_name="Biscuit API35",
            booted=False, runtime="API 35",
        )
        good = _make_sim(
            sim_id="Pixel8_API34", platform="android", device_name="Pixel8 API34",
            booted=False, runtime="API 34",
        )
        mock_find.return_value = bad
        mock_find_matching.return_value = [bad, good]
        mock_boot.side_effect = [RuntimeError("snapshot pending"), None]

        session = claim(ClaimSpec(platform="android", visible=True))

        self.assertEqual(session.sim_id, "Pixel8_API34")
        self.assertEqual(mock_boot.call_args_list[0], call("Biscuit_API35", headless=False))
        self.assertEqual(mock_boot.call_args_list[1], call("Pixel8_API34", headless=False))

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.android.shutdown")
    @patch("simemu.session.android.boot")
    @patch("simemu.session.android.wait_until_ready", side_effect=[RuntimeError("adb device unauthorized"), "emulator-5556"])
    @patch("simemu.session.android.get_android_serial", return_value="emulator-5556")
    @patch("simemu.session.find_matching_devices")
    @patch("simemu.session.find_best_device")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_falls_back_when_booted_android_candidate_is_not_adb_ready(
        self,
        mock_maint,
        mock_find,
        mock_find_matching,
        mock_get_serial,
        mock_ready,
        mock_boot,
        mock_shutdown,
        mock_win,
    ) -> None:
        bad = _make_sim(
            sim_id="Broken_API35", platform="android", device_name="Broken API35",
            booted=True, runtime="API 35",
        )
        good = _make_sim(
            sim_id="Healthy_API34", platform="android", device_name="Healthy API34",
            booted=True, runtime="API 34",
        )
        mock_find.return_value = bad
        mock_find_matching.return_value = [bad, good]

        session = claim(ClaimSpec(platform="android"))

        self.assertEqual(session.sim_id, "Broken_API35")
        mock_shutdown.assert_called_once_with("Broken_API35")
        mock_boot.assert_called_once_with("Broken_API35", headless=True)

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.android.wait_until_ready", return_value="emulator-5554")
    @patch("simemu.session.android.validate_serial", return_value=False)
    @patch("simemu.session.android.get_android_serial")
    @patch("simemu.session.find_best_device")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_expires_stale_active_android_session_before_reusing_port(
        self,
        mock_maint,
        mock_find,
        mock_get_serial,
        mock_validate,
        mock_ready,
        mock_win,
    ) -> None:
        stale_heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=ANDROID_DISCONNECT_GRACE + 5)).isoformat()
        self._seed_session(
            "s-old",
            sim_id="MedPhone_API34",
            pinned_serial="emulator-5554",
            heartbeat_at=stale_heartbeat,
            expires_at=_compute_expires_at("active", stale_heartbeat),
        )
        mock_find.return_value = _make_sim(
            sim_id="Okra_API35",
            platform="android",
            device_name="Okra API35",
            booted=True,
            runtime="API 35",
        )
        mock_get_serial.side_effect = [None]

        session = claim(ClaimSpec(platform="android"))

        self.assertEqual(session.sim_id, "Okra_API35")
        self.assertEqual(session.pinned_serial, "emulator-5554")
        stale = get_session("s-old")
        self.assertEqual(stale.status, "expired")
        self.assertIsNone(stale.pinned_serial)

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.find_best_device")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_rejects_duplicate_sim_id(self, mock_maint, mock_find, mock_boot, mock_win) -> None:
        mock_find.return_value = _make_sim(booted=True)
        spec = ClaimSpec(platform="ios")
        claim(spec)

        # Second claim for same device should fail
        mock_find.return_value = _make_sim(booted=True)
        with self.assertRaises(SessionError) as ctx:
            claim(spec)
        self.assertEqual(ctx.exception.error_type, "device_already_claimed")
        error_json = ctx.exception.to_json()
        self.assertIsNone(error_json["session"])
        self.assertIn("existing_session", error_json)

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.find_best_device")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_expires_effectively_expired_duplicate_before_reusing_device(
        self,
        mock_maint,
        mock_find,
        mock_boot,
        mock_win,
    ) -> None:
        expired_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self._seed_session(
            "s-old",
            platform="ios",
            sim_id="AAA-111",
            device_name="iPhone 16 Pro",
            status="parked",
            heartbeat_at=(datetime.now(timezone.utc) - timedelta(seconds=EXPIRE_TIMEOUT + 5)).isoformat(),
            expires_at=expired_at,
        )
        mock_find.return_value = _make_sim(booted=True)

        session = claim(ClaimSpec(platform="ios"))

        self.assertEqual(session.sim_id, "AAA-111")
        stale = get_session("s-old")
        self.assertEqual(stale.status, "expired")

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.find_best_device")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_applies_window_mode(self, mock_maint, mock_find, mock_boot, mock_win) -> None:
        mock_find.return_value = _make_sim(booted=True)
        spec = ClaimSpec(platform="ios", visible=False)
        claim(spec)
        mock_win.assert_called_once_with("AAA-111", "ios", "iPhone 16 Pro")

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.find_best_device")
    @patch("simemu.session.state.check_maintenance")
    def test_claim_skips_window_mode_when_visible(self, mock_maint, mock_find, mock_boot, mock_win) -> None:
        mock_find.return_value = _make_sim(booted=True)
        spec = ClaimSpec(platform="ios", visible=True)
        claim(spec)
        mock_win.assert_not_called()


class TestTouch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="simemu-touch-test-")
        self._old_state = os.environ.get("SIMEMU_STATE_DIR")
        self._old_config = os.environ.get("SIMEMU_CONFIG_DIR")
        os.environ["SIMEMU_STATE_DIR"] = self.tmpdir.name
        os.environ["SIMEMU_CONFIG_DIR"] = self.tmpdir.name

    def tearDown(self) -> None:
        if self._old_state is None:
            os.environ.pop("SIMEMU_STATE_DIR", None)
        else:
            os.environ["SIMEMU_STATE_DIR"] = self._old_state
        if self._old_config is None:
            os.environ.pop("SIMEMU_CONFIG_DIR", None)
        else:
            os.environ["SIMEMU_CONFIG_DIR"] = self._old_config
        self.tmpdir.cleanup()

    def _seed_session(self, session_id: str = "s-aaa111", **overrides) -> None:
        now = _now_iso()
        session_data = {
            "session_id": session_id,
            "platform": "ios",
            "form_factor": "phone",
            "os_version": None,
            "real_device": False,
            "label": "",
            "status": "active",
            "sim_id": "AAA-111",
            "device_name": "iPhone 16 Pro",
            "agent": "test",
            "created_at": now,
            "heartbeat_at": now,
            "expires_at": _compute_expires_at("active", now),
            "resolved_os_version": "iOS 26.2",
            "claim_platform": "ios",
            "claim_form_factor": "phone",
            "claim_os_version": None,
            "claim_real_device": False,
            "claim_label": "",
        }
        session_data.update(overrides)
        sf = Path(self.tmpdir.name) / "sessions.json"
        data = {"sessions": {session_id: session_data}}
        sf.write_text(json.dumps(data))

    @patch("simemu.session.android.get_android_serial", return_value="emulator-5554")
    def test_touch_updates_heartbeat(self, mock_serial) -> None:
        self._seed_session()
        old = get_session("s-aaa111")
        time.sleep(0.01)
        updated = touch("s-aaa111")
        self.assertNotEqual(old.heartbeat_at, updated.heartbeat_at)
        self.assertEqual(updated.status, "active")

    @patch("simemu.session.android.get_android_serial", return_value="emulator-5554")
    def test_touch_allows_active_session_past_idle_transition_before_terminal_expiry(self, mock_serial) -> None:
        stale_but_not_terminal = (datetime.now(timezone.utc) - timedelta(seconds=IDLE_TIMEOUT + 60)).isoformat()
        self._seed_session(
            heartbeat_at=stale_but_not_terminal,
            expires_at=_compute_expires_at("active", stale_but_not_terminal),
        )
        updated = touch("s-aaa111")
        self.assertEqual(updated.status, "active")
        self.assertGreater(datetime.fromisoformat(updated.heartbeat_at), datetime.fromisoformat(stale_but_not_terminal))

    @patch("simemu.session.ios.boot")
    def test_touch_reboots_parked_session(self, mock_boot) -> None:
        self._seed_session(status="parked")
        session = touch("s-aaa111")
        mock_boot.assert_called_once_with("AAA-111")
        self.assertEqual(session.status, "active")

    @patch("simemu.session.android.boot")
    def test_touch_reboots_parked_android_session(self, mock_boot) -> None:
        self._seed_session(status="parked", platform="android", sim_id="Pixel_7")
        session = touch("s-aaa111")
        mock_boot.assert_called_once_with("Pixel_7", headless=True)
        self.assertEqual(session.status, "active")

    @patch("simemu.session.android.get_android_serial", return_value="emulator-5554")
    def test_touch_pins_android_serial_when_missing(self, mock_serial) -> None:
        self._seed_session(status="active", platform="android", sim_id="Pixel_7", pinned_serial=None)
        session = touch("s-aaa111")
        stored = get_session("s-aaa111")
        self.assertEqual(session.pinned_serial, "emulator-5554")
        self.assertEqual(stored.pinned_serial, "emulator-5554")

    @patch.dict(os.environ, {"SIMEMU_DISABLE_SESSION_AUTO_REBOOT": "1"})
    @patch("simemu.session.android.get_android_serial", side_effect=[None, "emulator-5554"])
    def test_touch_waits_for_transient_android_serial_loss_during_no_reboot_proof(self, mock_serial) -> None:
        self._seed_session(status="active", platform="android", sim_id="Pixel_7", pinned_serial=None)
        session = touch("s-aaa111")
        self.assertEqual(session.pinned_serial, "emulator-5554")
        self.assertEqual(mock_serial.call_count, 2)

    @patch.dict(os.environ, {"SIMEMU_DISABLE_SESSION_AUTO_REBOOT": "1"})
    @patch("simemu.session.time.sleep", return_value=None)
    @patch("subprocess.run")
    @patch("simemu.session.android.get_android_serial", side_effect=[None, None, None])
    def test_touch_no_reboot_keeps_pinned_serial_on_transient_avd_lookup_loss(
        self,
        mock_serial,
        mock_run,
        mock_sleep,
    ) -> None:
        self._seed_session(
            status="active",
            platform="android",
            sim_id="Pixel_7",
            pinned_serial="emulator-5554",
        )
        session = touch("s-aaa111")

        self.assertEqual(session.status, "active")
        self.assertEqual(session.pinned_serial, "emulator-5554")
        mock_run.assert_called_once_with(
            ["adb", "-s", "emulator-5554", "reconnect"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(mock_serial.call_count, 3)

    @patch.dict(os.environ, {"SIMEMU_DISABLE_SESSION_AUTO_REBOOT": "1"})
    @patch("simemu.session.android.boot")
    def test_touch_refuses_auto_reboot_when_disabled_for_proof(self, mock_boot) -> None:
        self._seed_session(status="parked", platform="android", sim_id="Pixel_7")
        with self.assertRaises(RuntimeError) as ctx:
            touch("s-aaa111")
        self.assertIn("would auto-reboot", str(ctx.exception))
        mock_boot.assert_not_called()

    def test_touch_raises_for_expired_session(self) -> None:
        self._seed_session(status="expired")
        with self.assertRaises(SessionError) as ctx:
            touch("s-aaa111")
        self.assertEqual(ctx.exception.error_type, "session_expired")

    def test_touch_raises_with_pid_dead_reason_reveals_true_cause(self) -> None:
        """T-054/T-LU-054: a PID-reaped claim must say so, not blame inactivity.

        A blanket "expired after inactivity" hint is actively misleading when
        the real cause was a liveness reap — the holder may have been working
        the entire time. The error must carry the real reason so an agent
        doesn't misdiagnose it as having been idle too long.
        """
        self._seed_session(status="expired", reaped_reason="claimant_pid_dead")
        with self.assertRaises(SessionError) as ctx:
            touch("s-aaa111")
        self.assertEqual(ctx.exception.error_type, "session_expired")
        j = ctx.exception.to_json()
        self.assertEqual(j["reaped_reason"], "claimant_pid_dead")
        self.assertIn("not due to inactivity", j["hint"])

    def test_touch_raises_with_idle_timeout_reason_by_default(self) -> None:
        """A session that crosses the terminal idle ceiling gets the generic
        inactivity hint and is persisted with reaped_reason=idle_timeout —
        distinct from a PID-death reap even though both end in session_expired.
        """
        stale_heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=EXPIRE_TIMEOUT + 5)).isoformat()
        self._seed_session(heartbeat_at=stale_heartbeat, expires_at=stale_heartbeat)
        with self.assertRaises(SessionError) as ctx:
            touch("s-aaa111")
        j = ctx.exception.to_json()
        self.assertEqual(j["reaped_reason"], "idle_timeout")
        self.assertIn("inactivity", j["hint"])
        self.assertNotIn("not due to inactivity", j["hint"])
        raw = json.loads((Path(self.tmpdir.name) / "sessions.json").read_text())
        self.assertEqual(raw["sessions"]["s-aaa111"]["reaped_reason"], "idle_timeout")

    def test_touch_raises_for_nonexistent_session(self) -> None:
        with self.assertRaises(SessionError) as ctx:
            touch("s-doesnt-exist")
        self.assertEqual(ctx.exception.error_type, "session_not_found")

    def test_touch_raises_for_released_session(self) -> None:
        self._seed_session(status="released")
        with self.assertRaises(SessionError) as ctx:
            touch("s-aaa111")
        self.assertEqual(ctx.exception.error_type, "session_released")


class TestRealDeviceRecovery(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="simemu-real-test-")
        self._old_state = os.environ.get("SIMEMU_STATE_DIR")
        self._old_config = os.environ.get("SIMEMU_CONFIG_DIR")
        os.environ["SIMEMU_STATE_DIR"] = self.tmpdir.name
        os.environ["SIMEMU_CONFIG_DIR"] = self.tmpdir.name

    def tearDown(self) -> None:
        if self._old_state is None:
            os.environ.pop("SIMEMU_STATE_DIR", None)
        else:
            os.environ["SIMEMU_STATE_DIR"] = self._old_state
        if self._old_config is None:
            os.environ.pop("SIMEMU_CONFIG_DIR", None)
        else:
            os.environ["SIMEMU_CONFIG_DIR"] = self._old_config
        self.tmpdir.cleanup()

    def _seed_real_session(self, platform="ios") -> None:
        from simemu.session import _write_sessions_raw, _now_iso, _compute_expires_at
        now = _now_iso()
        _write_sessions_raw({"sessions": {
            "s-real01": {
                "session_id": "s-real01", "platform": platform,
                "form_factor": "phone", "os_version": None,
                "real_device": True, "label": "", "status": "active",
                "sim_id": "REAL-UDID-001", "device_name": "iPhone 15 (real)",
                "agent": "test", "created_at": now, "heartbeat_at": now,
                "expires_at": _compute_expires_at("active", now),
                "resolved_os_version": "iOS 18.2",
                "claim_platform": platform, "claim_form_factor": "phone",
                "claim_os_version": None, "claim_real_device": True, "claim_label": "",
            }
        }})

    @patch("simemu.session.device.list_android_devices", return_value=[])
    def test_touch_raises_when_real_android_disconnected(self, mock_list_devices) -> None:
        self._seed_real_session(platform="android")
        with self.assertRaises(RuntimeError) as ctx:
            touch("s-real01")
        self.assertIn("no longer connected", str(ctx.exception))

    @patch("simemu.session.device.list_android_devices", return_value=[MagicMock(device_id="REAL-UDID-001")])
    def test_touch_succeeds_when_real_android_connected(self, mock_list_devices) -> None:
        self._seed_real_session(platform="android")
        session = touch("s-real01")
        self.assertEqual(session.status, "active")

    @patch("simemu.discover.list_real_ios", return_value=[])
    def test_touch_raises_when_real_ios_disconnected(self, mock_list) -> None:
        self._seed_real_session(platform="ios")
        with self.assertRaises(RuntimeError) as ctx:
            touch("s-real01")
        self.assertIn("no longer connected", str(ctx.exception))


class TestRenew(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="simemu-renew-test-")
        self._old_state = os.environ.get("SIMEMU_STATE_DIR")
        self._old_config = os.environ.get("SIMEMU_CONFIG_DIR")
        os.environ["SIMEMU_STATE_DIR"] = self.tmpdir.name
        os.environ["SIMEMU_CONFIG_DIR"] = self.tmpdir.name

    def tearDown(self) -> None:
        if self._old_state is None:
            os.environ.pop("SIMEMU_STATE_DIR", None)
        else:
            os.environ["SIMEMU_STATE_DIR"] = self._old_state
        if self._old_config is None:
            os.environ.pop("SIMEMU_CONFIG_DIR", None)
        else:
            os.environ["SIMEMU_CONFIG_DIR"] = self._old_config
        self.tmpdir.cleanup()

    def _seed_session(self, session_id: str = "s-aaa111", **overrides) -> None:
        now = _now_iso()
        session_data = {
            "session_id": session_id,
            "platform": "ios",
            "form_factor": "phone",
            "os_version": None,
            "real_device": False,
            "label": "",
            "status": "active",
            "sim_id": "AAA-111",
            "device_name": "iPhone 16 Pro",
            "agent": "test",
            "created_at": now,
            "heartbeat_at": now,
            "expires_at": _compute_expires_at("active", now),
            "resolved_os_version": "iOS 26.2",
            "claim_platform": "ios",
            "claim_form_factor": "phone",
            "claim_os_version": None,
            "claim_real_device": False,
            "claim_label": "",
        }
        session_data.update(overrides)
        sf = Path(self.tmpdir.name) / "sessions.json"
        data = {"sessions": {session_id: session_data}}
        sf.write_text(json.dumps(data))

    def test_renew_extends_session(self) -> None:
        self._seed_session()
        old = get_session("s-aaa111")
        time.sleep(0.01)
        renewed = renew("s-aaa111")
        self.assertNotEqual(old.heartbeat_at, renewed.heartbeat_at)
        self.assertEqual(renewed.status, "active")


class TestRelease(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="simemu-release-test-")
        self._old_state = os.environ.get("SIMEMU_STATE_DIR")
        self._old_config = os.environ.get("SIMEMU_CONFIG_DIR")
        os.environ["SIMEMU_STATE_DIR"] = self.tmpdir.name
        os.environ["SIMEMU_CONFIG_DIR"] = self.tmpdir.name

    def tearDown(self) -> None:
        if self._old_state is None:
            os.environ.pop("SIMEMU_STATE_DIR", None)
        else:
            os.environ["SIMEMU_STATE_DIR"] = self._old_state
        if self._old_config is None:
            os.environ.pop("SIMEMU_CONFIG_DIR", None)
        else:
            os.environ["SIMEMU_CONFIG_DIR"] = self._old_config
        self.tmpdir.cleanup()

    def _seed_session(self, session_id: str = "s-aaa111", **overrides) -> None:
        now = _now_iso()
        session_data = {
            "session_id": session_id,
            "platform": "ios",
            "form_factor": "phone",
            "os_version": None,
            "real_device": False,
            "label": "",
            "status": "active",
            "sim_id": "AAA-111",
            "device_name": "iPhone 16 Pro",
            "agent": "test",
            "created_at": now,
            "heartbeat_at": now,
            "expires_at": _compute_expires_at("active", now),
            "resolved_os_version": "iOS 26.2",
            "claim_platform": "ios",
            "claim_form_factor": "phone",
            "claim_os_version": None,
            "claim_real_device": False,
            "claim_label": "",
        }
        session_data.update(overrides)
        sf = Path(self.tmpdir.name) / "sessions.json"
        data = {"sessions": {session_id: session_data}}
        sf.write_text(json.dumps(data))

    def test_release_sets_status_released(self) -> None:
        self._seed_session()
        session = release("s-aaa111")
        self.assertEqual(session.status, "released")
        # Verify persisted too
        persisted = get_session("s-aaa111")
        self.assertEqual(persisted.status, "released")
        raw = _read_sessions_raw()["sessions"]["s-aaa111"]
        self.assertIn("released_at", raw)
        self.assertIn("released_by", raw)

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.erase")
    def test_release_cleans_up_ios_simulator_state(self, mock_erase, mock_window) -> None:
        self._seed_session(visible=True, last_build_artifact="/tmp/build/App.app")
        release("s-aaa111")

        mock_erase.assert_called_once_with("AAA-111")
        mock_window.assert_called_once_with("AAA-111", "ios", "iPhone 16 Pro")

        persisted = json.loads((Path(self.tmpdir.name) / "sessions.json").read_text())
        saved = persisted["sessions"]["s-aaa111"]
        self.assertFalse(saved["visible"])
        self.assertNotIn("last_build_artifact", saved)

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.android.erase")
    def test_release_cleans_up_android_simulator_state(self, mock_erase, mock_window) -> None:
        self._seed_session(
            session_id="s-droid1",
            platform="android",
            sim_id="Pixel_7",
            device_name="Pixel 7",
        )
        release("s-droid1")

        mock_erase.assert_called_once_with("Pixel_7")
        mock_window.assert_called_once_with("Pixel_7", "android", "Pixel 7")

    def test_release_raises_for_nonexistent(self) -> None:
        with self.assertRaises(SessionError) as ctx:
            release("s-nosuch")
        self.assertEqual(ctx.exception.error_type, "session_not_found")


class TestGetSession(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="simemu-get-test-")
        self._old_state = os.environ.get("SIMEMU_STATE_DIR")
        os.environ["SIMEMU_STATE_DIR"] = self.tmpdir.name

    def tearDown(self) -> None:
        if self._old_state is None:
            os.environ.pop("SIMEMU_STATE_DIR", None)
        else:
            os.environ["SIMEMU_STATE_DIR"] = self._old_state
        self.tmpdir.cleanup()

    def test_returns_none_for_missing(self) -> None:
        result = get_session("s-nonexistent")
        self.assertIsNone(result)


class TestGetActiveSessions(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="simemu-active-test-")
        self._old_state = os.environ.get("SIMEMU_STATE_DIR")
        os.environ["SIMEMU_STATE_DIR"] = self.tmpdir.name

    def tearDown(self) -> None:
        if self._old_state is None:
            os.environ.pop("SIMEMU_STATE_DIR", None)
        else:
            os.environ["SIMEMU_STATE_DIR"] = self._old_state
        self.tmpdir.cleanup()

    def test_filters_expired(self) -> None:
        now = _now_iso()
        base = {
            "platform": "ios",
            "form_factor": "phone",
            "os_version": None,
            "real_device": False,
            "label": "",
            "sim_id": "AAA-111",
            "device_name": "iPhone 16 Pro",
            "agent": "test",
            "created_at": now,
            "heartbeat_at": now,
            "resolved_os_version": "iOS 26.2",
            "claim_platform": "ios",
            "claim_form_factor": "phone",
            "claim_os_version": None,
            "claim_real_device": False,
            "claim_label": "",
        }
        sf = Path(self.tmpdir.name) / "sessions.json"
        fresh_expires_at = _compute_expires_at("active", now)
        data = {
            "sessions": {
                "s-active": {**base, "session_id": "s-active", "status": "active",
                             "expires_at": fresh_expires_at},
                "s-idle": {**base, "session_id": "s-idle", "status": "idle",
                           "sim_id": "BBB-222", "expires_at": _compute_expires_at("idle", now)},
                "s-expired": {**base, "session_id": "s-expired", "status": "expired",
                              "sim_id": "CCC-333", "expires_at": now},
                "s-released": {**base, "session_id": "s-released", "status": "released",
                               "sim_id": "DDD-444", "expires_at": now},
                "s-parked": {**base, "session_id": "s-parked", "status": "parked",
                             "sim_id": "EEE-555", "expires_at": _compute_expires_at("parked", now)},
            }
        }
        sf.write_text(json.dumps(data))

        active = get_active_sessions()
        self.assertIn("s-active", active)
        self.assertIn("s-idle", active)
        self.assertIn("s-parked", active)
        self.assertNotIn("s-expired", active)
        self.assertNotIn("s-released", active)

    def test_keeps_active_sessions_past_idle_transition_before_terminal_expiry(self) -> None:
        now = _now_iso()
        stale_but_not_terminal = (datetime.now(timezone.utc) - timedelta(seconds=IDLE_TIMEOUT + 60)).isoformat()
        base = {
            "platform": "ios",
            "form_factor": "phone",
            "os_version": None,
            "real_device": False,
            "label": "",
            "sim_id": "AAA-111",
            "device_name": "iPhone 16 Pro",
            "agent": "test",
            "created_at": now,
            "heartbeat_at": now,
            "resolved_os_version": "iOS 26.2",
            "claim_platform": "ios",
            "claim_form_factor": "phone",
            "claim_os_version": None,
            "claim_real_device": False,
            "claim_label": "",
        }
        sf = Path(self.tmpdir.name) / "sessions.json"
        sf.write_text(json.dumps({
            "sessions": {
                "s-active-fresh": {
                    **base,
                    "session_id": "s-active-fresh",
                    "status": "active",
                    "expires_at": _compute_expires_at("active", now),
                },
                "s-active-stale": {
                    **base,
                    "session_id": "s-active-stale",
                    "status": "active",
                    "sim_id": "BBB-222",
                    "heartbeat_at": stale_but_not_terminal,
                    "expires_at": _compute_expires_at("active", stale_but_not_terminal),
                },
            }
        }))

        active = get_active_sessions()
        self.assertIn("s-active-fresh", active)
        self.assertIn("s-active-stale", active)

    def test_require_session_marks_effectively_expired_session(self) -> None:
        stale_heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=EXPIRE_TIMEOUT + 60)).isoformat()
        sf = Path(self.tmpdir.name) / "sessions.json"
        sf.write_text(json.dumps({
            "sessions": {
                "s-expiring": {
                    "session_id": "s-expiring",
                    "platform": "ios",
                    "form_factor": "phone",
                    "os_version": None,
                    "real_device": False,
                    "label": "",
                    "status": "active",
                    "sim_id": "AAA-111",
                    "device_name": "iPhone 16 Pro",
                    "agent": "test",
                    "created_at": stale_heartbeat,
                    "heartbeat_at": stale_heartbeat,
                    "expires_at": _compute_expires_at("active", stale_heartbeat),
                    "resolved_os_version": "iOS 26.2",
                    "claim_platform": "ios",
                    "claim_form_factor": "phone",
                    "claim_os_version": None,
                    "claim_real_device": False,
                    "claim_label": "",
                }
            }
        }))

        with self.assertRaises(SessionError) as ctx:
            require_session("s-expiring")
        self.assertEqual(ctx.exception.error_type, "session_expired")

        data = json.loads(sf.read_text())
        self.assertEqual(data["sessions"]["s-expiring"]["status"], "expired")


class TestLifecycleTick(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="simemu-lifecycle-test-")
        self._old_state = os.environ.get("SIMEMU_STATE_DIR")
        os.environ["SIMEMU_STATE_DIR"] = self.tmpdir.name

    def tearDown(self) -> None:
        if self._old_state is None:
            os.environ.pop("SIMEMU_STATE_DIR", None)
        else:
            os.environ["SIMEMU_STATE_DIR"] = self._old_state
        self.tmpdir.cleanup()

    def _seed(self, session_id: str, status: str, idle_seconds: float) -> None:
        heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=idle_seconds)).isoformat()
        now = _now_iso()
        session_data = {
            "session_id": session_id,
            "platform": "ios",
            "form_factor": "phone",
            "os_version": None,
            "real_device": False,
            "label": "",
            "status": status,
            "sim_id": f"SIM-{session_id}",
            "device_name": "iPhone 16 Pro",
            "agent": "test",
            "created_at": now,
            "heartbeat_at": heartbeat,
            "expires_at": _compute_expires_at(status, heartbeat),
            "resolved_os_version": "iOS 26.2",
            "claim_platform": "ios",
            "claim_form_factor": "phone",
            "claim_os_version": None,
            "claim_real_device": False,
            "claim_label": "",
        }
        sf = Path(self.tmpdir.name) / "sessions.json"
        if sf.exists():
            data = json.loads(sf.read_text())
        else:
            data = {"sessions": {}}
        data["sessions"][session_id] = session_data
        sf.write_text(json.dumps(data))

    def test_transitions_active_to_idle(self) -> None:
        self._seed("s-test", "active", IDLE_TIMEOUT + 60)
        changed = lifecycle_tick()
        self.assertIn("s-test", changed)
        session = get_session("s-test")
        self.assertEqual(session.status, "idle")

    @patch("simemu.session.ios.shutdown")
    def test_transitions_idle_to_parked(self, mock_shutdown) -> None:
        self._seed("s-test", "idle", IDLE_TIMEOUT + PARK_TIMEOUT + 60)
        changed = lifecycle_tick()
        self.assertIn("s-test", changed)
        session = get_session("s-test")
        self.assertEqual(session.status, "parked")

    @patch("simemu.session.ios.shutdown")
    def test_parks_and_shuts_down(self, mock_shutdown) -> None:
        self._seed("s-test", "idle", IDLE_TIMEOUT + PARK_TIMEOUT + 60)
        lifecycle_tick()
        mock_shutdown.assert_called_once_with("SIM-s-test")

    @patch("simemu.session.android.get_android_serial", return_value=None)
    def test_keeps_parked_android_session_and_clears_stale_pin(self, mock_get_serial) -> None:
        self._seed(
            "s-android",
            "parked",
            60,
        )
        sf = Path(self.tmpdir.name) / "sessions.json"
        data = json.loads(sf.read_text())
        data["sessions"]["s-android"].update({
            "platform": "android",
            "sim_id": "Okra_API35",
            "device_name": "Okra API35",
            "resolved_os_version": "API 35",
            "claim_platform": "android",
            "pinned_serial": "emulator-5554",
        })
        sf.write_text(json.dumps(data))

        changed = lifecycle_tick()

        self.assertIn("s-android", changed)
        session = get_session("s-android")
        self.assertEqual(session.status, "parked")
        self.assertIsNone(session.pinned_serial)

    @patch("simemu.session.android.get_android_serial", return_value=None)
    @patch("simemu.session.android.validate_serial", return_value=False)
    def test_expires_active_android_session_when_pin_is_stale_and_avd_is_gone(
        self,
        mock_validate,
        mock_get_serial,
    ) -> None:
        self._seed("s-android", "active", ANDROID_DISCONNECT_GRACE + 5)
        sf = Path(self.tmpdir.name) / "sessions.json"
        data = json.loads(sf.read_text())
        data["sessions"]["s-android"].update({
            "platform": "android",
            "sim_id": "MedPhone_API34",
            "device_name": "MedPhone API34",
            "resolved_os_version": "API 34",
            "claim_platform": "android",
            "pinned_serial": "emulator-5554",
        })
        sf.write_text(json.dumps(data))

        changed = lifecycle_tick()

        self.assertIn("s-android", changed)
        session = get_session("s-android")
        self.assertEqual(session.status, "expired")
        self.assertIsNone(session.pinned_serial)

    @patch("simemu.session.android.get_android_serial", return_value=None)
    @patch("simemu.session.android.validate_serial", return_value=False)
    def test_keeps_recent_active_android_session_when_adb_temporarily_missing(
        self,
        mock_validate,
        mock_get_serial,
    ) -> None:
        self._seed("s-android", "active", 10)
        sf = Path(self.tmpdir.name) / "sessions.json"
        data = json.loads(sf.read_text())
        data["sessions"]["s-android"].update({
            "platform": "android",
            "sim_id": "MedPhone_API34",
            "device_name": "MedPhone API34",
            "resolved_os_version": "API 34",
            "claim_platform": "android",
            "pinned_serial": "emulator-5554",
        })
        sf.write_text(json.dumps(data))

        changed = lifecycle_tick()

        self.assertNotIn("s-android", changed)
        session = get_session("s-android")
        self.assertEqual(session.status, "active")
        self.assertEqual(session.pinned_serial, "emulator-5554")

    def test_expires_old_sessions(self) -> None:
        self._seed("s-test", "active", EXPIRE_TIMEOUT + 60)
        changed = lifecycle_tick()
        self.assertIn("s-test", changed)
        session = get_session("s-test")
        self.assertEqual(session.status, "expired")

    def test_skips_already_expired(self) -> None:
        self._seed("s-test", "expired", EXPIRE_TIMEOUT + 1000)
        changed = lifecycle_tick()
        self.assertNotIn("s-test", changed)

    def test_skips_already_released(self) -> None:
        self._seed("s-test", "released", EXPIRE_TIMEOUT + 1000)
        changed = lifecycle_tick()
        self.assertNotIn("s-test", changed)

    def test_no_transition_when_recent(self) -> None:
        self._seed("s-test", "active", 10)  # 10 seconds ago
        changed = lifecycle_tick()
        self.assertEqual(changed, [])


class TestDoCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="simemu-do-test-")
        self._old_state = os.environ.get("SIMEMU_STATE_DIR")
        self._old_config = os.environ.get("SIMEMU_CONFIG_DIR")
        os.environ["SIMEMU_STATE_DIR"] = self.tmpdir.name
        os.environ["SIMEMU_CONFIG_DIR"] = self.tmpdir.name

    def tearDown(self) -> None:
        if self._old_state is None:
            os.environ.pop("SIMEMU_STATE_DIR", None)
        else:
            os.environ["SIMEMU_STATE_DIR"] = self._old_state
        if self._old_config is None:
            os.environ.pop("SIMEMU_CONFIG_DIR", None)
        else:
            os.environ["SIMEMU_CONFIG_DIR"] = self._old_config
        self.tmpdir.cleanup()

    def _seed_session(self, session_id: str = "s-aaa111", **overrides) -> None:
        now = _now_iso()
        session_data = {
            "session_id": session_id,
            "platform": "ios",
            "form_factor": "phone",
            "os_version": None,
            "real_device": False,
            "label": "",
            "status": "active",
            "sim_id": "AAA-111",
            "device_name": "iPhone 16 Pro",
            "agent": "test",
            "created_at": now,
            "heartbeat_at": now,
            "expires_at": _compute_expires_at("active", now),
            "resolved_os_version": "iOS 26.2",
            "claim_platform": "ios",
            "claim_form_factor": "phone",
            "claim_os_version": None,
            "claim_real_device": False,
            "claim_label": "",
        }
        session_data.update(overrides)
        sf = Path(self.tmpdir.name) / "sessions.json"
        data = {"sessions": {session_id: session_data}}
        sf.write_text(json.dumps(data))

    @patch("simemu.session.android.get_android_serial", return_value="emulator-5554")
    def test_do_command_done_releases(self, mock_serial) -> None:
        self._seed_session()
        result = do_command("s-aaa111", "done", [])
        self.assertEqual(result["status"], "released")
        session = get_session("s-aaa111")
        self.assertEqual(session.status, "released")

    @patch("simemu.session.android.get_android_serial", return_value="emulator-5554")
    def test_do_command_release_alias_releases(self, mock_serial) -> None:
        """T-056: 'release' is the discoverable verb name — same effect as 'done'."""
        self._seed_session()
        result = do_command("s-aaa111", "release", [])
        self.assertEqual(result["status"], "released")
        session = get_session("s-aaa111")
        self.assertEqual(session.status, "released")
        # No extra claim-call reaper needed to make the release visible.
        self.assertNotIn("s-aaa111", get_active_sessions())

    @patch("simemu.session.android.get_android_serial", return_value="emulator-5554")
    def test_do_command_boot_touches(self, mock_serial) -> None:
        self._seed_session()
        result = do_command("s-aaa111", "boot", [])
        self.assertIn("session", result)

    @patch("subprocess.run")
    @patch("simemu.session.android.get_android_serial", return_value="emulator-5554")
    def test_do_command_show_sets_visible(self, mock_serial, mock_run) -> None:
        self._seed_session()
        result = do_command("s-aaa111", "show", [])
        self.assertEqual(result["status"], "visible")

    @patch("simemu.session.android.get_android_serial", return_value="emulator-5558")
    @patch("simemu.session.android.boot")
    @patch("simemu.session.android.shutdown")
    @patch("simemu.session.android.current_window_frame", return_value=None)
    def test_do_command_show_reboots_android_window_if_missing(
        self,
        mock_window_frame,
        mock_shutdown,
        mock_boot,
        mock_serial,
    ) -> None:
        self._seed_session(
            platform="android",
            sim_id="Pixel_7",
            device_name="Pixel 7",
            pinned_serial="emulator-5554",
        )
        result = do_command("s-aaa111", "show", [])
        self.assertEqual(result["status"], "visible")
        mock_shutdown.assert_called_once_with("Pixel_7")
        mock_boot.assert_called_once_with("Pixel_7", headless=False)
        persisted = get_session("s-aaa111")
        self.assertEqual(persisted.pinned_serial, "emulator-5558")

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.android.get_android_serial", return_value="emulator-5554")
    def test_do_command_hide_sets_invisible(self, mock_serial, mock_win) -> None:
        self._seed_session()
        result = do_command("s-aaa111", "hide", [])
        self.assertEqual(result["status"], "invisible")

    @patch("simemu.session.android.tap")
    @patch("simemu.session.android.get_android_serial", return_value="emulator-5554")
    @patch("simemu.session.android.validate_serial", return_value=True)
    @patch("subprocess.run")
    def test_android_a11y_tap_uses_uiautomator_not_maestro(
        self,
        mock_run,
        mock_validate,
        mock_serial,
        mock_tap,
    ) -> None:
        self._seed_session(
            platform="android",
            sim_id="Pixel_7",
            device_name="Pixel 7",
            pinned_serial="emulator-5554",
        )

        xml = (
            '<hierarchy><node text="Settings" content-desc="" '
            'resource-id="" bounds="[100,200][300,400]" /></hierarchy>'
        )
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="UI hierchary dumped", stderr=""),
            MagicMock(returncode=0, stdout=xml, stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        result = do_command("s-aaa111", "a11y-tap", ["Settings"])

        self.assertEqual(result["status"], "tapped")
        self.assertEqual(result["method"], "uiautomator")
        self.assertEqual(result["x"], 200)
        self.assertEqual(result["y"], 300)
        mock_tap.assert_called_once_with("Pixel_7", 200, 300, pinned_serial="emulator-5554")
        flattened = [" ".join(call.args[0]) for call in mock_run.call_args_list]
        self.assertTrue(any("uiautomator dump" in cmd for cmd in flattened))
        self.assertFalse(any("maestro" in cmd for cmd in flattened))

    @patch("simemu.session.android.get_android_serial", return_value="emulator-5554")
    def test_do_command_renew(self, mock_serial) -> None:
        self._seed_session()
        result = do_command("s-aaa111", "renew", [])
        self.assertIn("session", result)
        self.assertEqual(result["status"], "active")

    @patch("simemu.session.android.get_android_serial", return_value="emulator-5554")
    def test_do_command_unknown_raises(self, mock_serial) -> None:
        self._seed_session()
        with self.assertRaises(RuntimeError) as ctx:
            do_command("s-aaa111", "fly-to-moon", [])
        self.assertIn("Unknown command", str(ctx.exception))
        self.assertIn("fly-to-moon", str(ctx.exception))


class TestSessionError(unittest.TestCase):
    def test_to_json(self) -> None:
        err = SessionError(
            error="session_not_found",
            session="s-abc123",
            hint="Session not found",
            custom_field="extra",
        )
        j = err.to_json()
        self.assertEqual(j["error"], "session_not_found")
        self.assertEqual(j["session"], "s-abc123")
        self.assertEqual(j["hint"], "Session not found")
        self.assertEqual(j["custom_field"], "extra")

    def test_is_runtime_error(self) -> None:
        err = SessionError(error="test", session="s-1", hint="test hint")
        self.assertIsInstance(err, RuntimeError)
        self.assertEqual(str(err), "test hint")


class TestMemoryBudget(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="simemu-mem-test-")
        self._old_state = os.environ.get("SIMEMU_STATE_DIR")
        self._old_config = os.environ.get("SIMEMU_CONFIG_DIR")
        self._old_budget = os.environ.get("SIMEMU_MEMORY_BUDGET_MB")
        os.environ["SIMEMU_STATE_DIR"] = self.tmpdir.name
        os.environ["SIMEMU_CONFIG_DIR"] = self.tmpdir.name

    def tearDown(self) -> None:
        if self._old_state is None:
            os.environ.pop("SIMEMU_STATE_DIR", None)
        else:
            os.environ["SIMEMU_STATE_DIR"] = self._old_state
        if self._old_config is None:
            os.environ.pop("SIMEMU_CONFIG_DIR", None)
        else:
            os.environ["SIMEMU_CONFIG_DIR"] = self._old_config
        if self._old_budget is None:
            os.environ.pop("SIMEMU_MEMORY_BUDGET_MB", None)
        else:
            os.environ["SIMEMU_MEMORY_BUDGET_MB"] = self._old_budget
        self.tmpdir.cleanup()

    def _seed_sessions(self, count: int, platform: str = "ios", status: str = "active") -> None:
        now = _now_iso()
        sf = Path(self.tmpdir.name) / "sessions.json"
        data = {"sessions": {}}
        for i in range(count):
            sid = f"s-mem{i:03d}"
            heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=i * 100)).isoformat()
            data["sessions"][sid] = {
                "session_id": sid,
                "platform": platform,
                "form_factor": "phone",
                "os_version": None,
                "real_device": False,
                "label": "",
                "status": status,
                "sim_id": f"SIM-{i}",
                "device_name": f"Device {i}",
                "agent": "test",
                "created_at": now,
                "heartbeat_at": heartbeat,
                "expires_at": _compute_expires_at(status, heartbeat),
                "resolved_os_version": "iOS 26.2",
                "claim_platform": platform,
                "claim_form_factor": "phone",
                "claim_os_version": None,
                "claim_real_device": False,
                "claim_label": "",
            }
        sf.write_text(json.dumps(data))

    @patch("simemu.session.window_mgr.apply_window_mode")
    @patch("simemu.session.ios.boot")
    @patch("simemu.session.find_best_device")
    @patch("simemu.session.state.check_maintenance")
    @patch("simemu.session.ios.shutdown")
    def test_memory_budget_enforcement(
        self, mock_shutdown, mock_maint, mock_find, mock_boot, mock_win
    ) -> None:
        # Set a very small budget: 4GB
        os.environ["SIMEMU_MEMORY_BUDGET_MB"] = "4096"
        # Seed 2 idle iOS sessions (2048 MB each = 4096 MB used)
        self._seed_sessions(2, platform="ios", status="idle")
        # Attempting to claim should park idle sessions to make room
        mock_find.return_value = _make_sim(sim_id="NEW-SIM", booted=True)
        spec = ClaimSpec(platform="ios")
        session = claim(spec)
        self.assertEqual(session.status, "active")
        # At least one shutdown should have been called
        self.assertTrue(mock_shutdown.called)


if __name__ == "__main__":
    unittest.main()
