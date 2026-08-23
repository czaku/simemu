"""
Session-based resource manager for simemu v2.

Agents interact with sessions (opaque IDs) instead of device slugs/UDIDs.
Sessions manage the full device lifecycle: claim → active → idle → parked → expired.

State file: ~/.simemu/sessions.json (separate from legacy state.json)
"""

import fcntl
import json
import os
import platform as _platform_mod
import re
import secrets
import signal
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import state, ios, android, device
from . import exclusive
from .discover import find_best_device, find_matching_devices, NoSimulatorAvailable
from . import window as window_mgr


# ── timeouts (seconds) ───────────────────────────────────────────────────────

IDLE_TIMEOUT = 20 * 60        # active → idle after 20min
PARK_TIMEOUT = 40 * 60        # idle → parked after 40min more (60min total)
EXPIRE_TIMEOUT = 2 * 60 * 60  # parked → expired after 2hr total idle
ANDROID_DISCONNECT_GRACE = int(os.environ.get("SIMEMU_ANDROID_DISCONNECT_GRACE", str(10 * 60)))  # tolerate transient adb loss before expiring
MAESTRO_TIMEOUT_SECONDS = int(os.environ.get("SIMEMU_MAESTRO_TIMEOUT_SECONDS", "300"))
MAESTRO_ANDROID_BRIDGE_PORT = int(os.environ.get("SIMEMU_ANDROID_MAESTRO_BRIDGE_PORT", "7001"))
MAESTRO_STALE_BRIDGE_SECONDS = int(os.environ.get("SIMEMU_MAESTRO_STALE_BRIDGE_SECONDS", "120"))

# Default memory budget in MB
DEFAULT_MEMORY_BUDGET_MB = 16 * 1024  # 16GB

# Per-device estimated memory usage in MB
_DEVICE_MEMORY_MB = {
    "ios": 2048,
    "android": 3072,
    "macos": 0,  # native — no VM overhead
}


# ── data model ───────────────────────────────────────────────────────────────

@dataclass
class ClaimSpec:
    platform: str                        # "ios" | "android" | "macos"
    form_factor: str = "phone"           # "phone" | "tablet" | "watch" | "tv" | "vision"
    os_version: str | None = None        # requested version or None (any)
    real_device: bool = False
    device_selector: str | None = None   # specific device id/name/alias to target
    label: str = ""
    visible: bool = False                # if True, keep window visible (default: headless)

    def to_claim_command(self) -> str:
        """Reconstruct the CLI command to re-claim with identical parameters."""
        parts = ["simemu", "claim", self.platform]
        if self.os_version:
            parts += ["--version", self.os_version]
        if self.form_factor != "phone":
            parts += ["--form-factor", self.form_factor]
        if self.real_device:
            parts.append("--real")
        if self.device_selector:
            parts += ["--device", self.device_selector]
        if self.visible:
            parts.append("--show")
        if self.label:
            parts += ["--label", f"'{self.label}'"]
        return " ".join(parts)


@dataclass
class Session:
    session_id: str                      # "s-" + 6 hex chars
    platform: str                        # "ios" | "android" | "macos"
    form_factor: str                     # "phone" | "tablet" | "watch" | "tv" | "vision"
    os_version: str | None               # requested version or None
    real_device: bool
    label: str
    status: str                          # "active" | "idle" | "parked" | "expired" | "released"
    sim_id: str                          # internal device ID (opaque to agent)
    device_name: str                     # internal device name
    agent: str                           # from SIMEMU_AGENT env
    created_at: str                      # ISO timestamp
    heartbeat_at: str                    # ISO timestamp — last activity
    expires_at: str | None = None        # ISO timestamp (computed)
    resolved_os_version: str | None = None  # actual OS version of assigned device

    # Pinned adb serial for Android session isolation — set at claim time,
    # validated before every command to prevent cross-session contamination
    pinned_serial: str | None = None

    # Exclusive-claim ownership tracking. `pid` is the PID of the claiming
    # process; used for stale-claim reaping when the owner dies without
    # releasing. `claim_pgid` is the process-group at claim time (useful for
    # killing the whole chain on cleanup). `claim_token` is an opaque secret
    # an agent can use to prove ownership of a session (validated via the
    # SIMEMU_SESSION_TOKEN env var when set).
    pid: int | None = None
    claim_pgid: int | None = None
    claim_token: str | None = None

    # Stored claim spec for error recovery messages
    claim_platform: str = ""
    claim_form_factor: str = "phone"
    claim_os_version: str | None = None
    claim_real_device: bool = False
    claim_device_selector: str | None = None
    claim_label: str = ""

    def to_agent_json(self) -> dict:
        """Return the JSON visible to agents (no internal device details)."""
        out = {
            "session": self.session_id,
            "platform": self.platform,
            "form_factor": self.form_factor,
            "os_version": self.resolved_os_version or self.os_version or "latest",
            "status": self.status,
            "label": self.label,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }
        if self.claim_token:
            out["claim_token"] = self.claim_token
        return out

    def claim_spec(self) -> ClaimSpec:
        """Reconstruct the ClaimSpec used to create this session."""
        return ClaimSpec(
            platform=self.claim_platform or self.platform,
            form_factor=self.claim_form_factor or self.form_factor,
            os_version=self.claim_os_version or self.os_version,
            real_device=self.claim_real_device or self.real_device,
            device_selector=self.claim_device_selector,
            label=self.claim_label or self.label,
        )

    def reclaim_command(self) -> str:
        """Return the exact CLI command to re-claim this session."""
        return self.claim_spec().to_claim_command()


def _gen_session_id() -> str:
    return f"s-{secrets.token_hex(3)}"


def _safe_pgid() -> int | None:
    """Return the current process group id, or None on platforms where it fails."""
    try:
        return os.getpgid(0)
    except OSError:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_FLOW_APP_ID_RE = re.compile(r"^\s*appId\s*:\s*['\"]?([^'\"#\s]+)")
_MAESTRO_ANDROID_DRIVER_BOOT_ERROR_RE = re.compile(
    r"Not able to reach the gRPC server while processing deviceInfo command"
    r"|Connection refused: localhost/(?:127\.0\.0\.1|\[[0-9a-fA-F:]+\]):7001"
    r"|Http2Exception: First received frame was not SETTINGS"
    r"|INTERNAL: http2 exception"
    r"|Failed to install apk .*maestro-app"
    r"|Install failed:"
)
_MAESTRO_ANDROID_TRANSPORT_LOSS_RE = re.compile(
    r"device ['\"]?emulator-\d+['\"]? not found"
    r"|device offline"
    r"|Device emulator-\d+ was requested, but it is not connected"
    r"|Command failed \(host:transport:emulator-\d+\)"
    r"|DEADLINE_EXCEEDED: deadline exceeded"
    r"|StatusRuntimeException: UNAVAILABLE"
    r"|StatusRuntimeException: RESOURCE_EXHAUSTED"
    r"|RESOURCE_EXHAUSTED:.*(?:screenshot|viewHierarchy|hierarchy|grpc|gRPC)"
    r"|java\.io\.EOFException"
    r"|AdbShellStream\.read"
    r"|gRPC keepalive timeout"
    r"|waiting_for_connection"
)


def _extract_maestro_app_id(flow_path: str) -> str | None:
    path = Path(flow_path)
    if not path.exists() or path.suffix not in {".yaml", ".yml"}:
        return None
    try:
        for line in path.read_text(errors="ignore").splitlines()[:40]:
            match = _FLOW_APP_ID_RE.match(line)
            if match:
                return match.group(1).strip()
    except OSError:
        return None
    return None


def _resolve_maestro_target_app(session_id: str, flow_files: list[str]) -> str | None:
    for flow_path in flow_files:
        app_id = _extract_maestro_app_id(flow_path)
        if app_id:
            return app_id

    data = _read_sessions_raw()
    return data["sessions"].get(session_id, {}).get("last_app")


def _android_package_name(app_id: str) -> str:
    """Normalize an Android package or component string to its package name."""
    return app_id.split("/", 1)[0]


def _ensure_maestro_target_foreground(
    session_id: str,
    platform: str,
    sim_id: str,
    expected_app: str | None,
    android_kwargs: dict,
) -> None:
    if not expected_app or expected_app == "com.apple.springboard":
        return

    provenance = get_provenance(session_id)
    launch_args = provenance.get("last_launch_args") or []
    if not isinstance(launch_args, list):
        launch_args = []

    if platform in ("ios", "watchos", "tvos", "visionos"):
        current = ios.foreground_app(sim_id)
        if current != expected_app:
            ios.launch(sim_id, expected_app, launch_args)
        if not ios.wait_for_foreground_app(sim_id, expected_app, timeout=4.0, delay=0.2):
            ios.accept_open_app_alert(sim_id, attempts=3, delay=0.25)
            if not ios.wait_for_foreground_app(sim_id, expected_app, timeout=2.0, delay=0.2):
                actual = ios.foreground_app(sim_id)
                raise RuntimeError(
                    f"Maestro handoff failed: expected '{expected_app}' foreground on iOS, got '{actual}'."
                )
        return

    expected_package = _android_package_name(expected_app)
    launch_target = expected_package
    last_launch_component = provenance.get("last_launch_component")
    if (
        isinstance(last_launch_component, str)
        and "/" in last_launch_component
        and last_launch_component.split("/", 1)[0] == expected_package
    ):
        launch_target = last_launch_component

    current = android.foreground_app(sim_id, retries=2, delay=0.5, **android_kwargs)
    if current != expected_package:
        resolved_component = android.launch(sim_id, launch_target, launch_args, **android_kwargs)
        if isinstance(resolved_component, str) and "/" in resolved_component:
            update_provenance(session_id, last_launch_component=resolved_component)
    actual = android.foreground_app(sim_id, retries=3, delay=0.75, **android_kwargs)
    if actual != expected_package:
        raise RuntimeError(
            f"Maestro handoff failed: expected '{expected_package}' foreground on Android, got '{actual}'."
        )


def _verify_or_repair_android_maestro_target(
    *,
    session_id: str,
    sim_id: str,
    expected_app: str | None,
    android_kwargs: dict,
) -> None:
    if not expected_app:
        return

    expected_package = _android_package_name(expected_app)
    try:
        android.verify_install(sim_id, expected_package, timeout=10, **android_kwargs)
        return
    except RuntimeError as exc:
        apk_path = _get_build_artifact(session_id)
        if not apk_path or not Path(apk_path).exists():
            raise RuntimeError(
                f"Android Maestro retry could not verify '{expected_package}' after adb transport loss, "
                f"and no reusable APK artifact is recorded for repair-install. {exc}"
            )
        android.repair_install(sim_id, expected_package, apk_path, timeout=90, **android_kwargs)


def _maestro_has_flag(args: list[str], flag: str) -> bool:
    return any(arg == flag or arg.startswith(f"{flag}=") for arg in args)


def _extract_maestro_option(args: list[str], flag: str) -> str | None:
    for idx, arg in enumerate(args):
        if arg == flag and idx + 1 < len(args):
            return args[idx + 1]
        if arg.startswith(f"{flag}="):
            return arg.split("=", 1)[1]
    return None


def _maestro_debug_output_dir(session_id: str) -> str:
    root = Path(os.environ.get("SIMEMU_OUTPUT_DIR", Path.home() / ".simemu"))
    root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = root / "maestro-debug" / f"{session_id}_{ts}"
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


def _maestro_env(platform: str) -> dict[str, str]:
    env = dict(os.environ)

    # Maestro's PostHog analytics queue thread is non-daemon: when the
    # analytics call stalls (filtered/slow network), the JVM never exits
    # after the flow finishes — every flow "hangs" until the caller's
    # timeout. Maestro itself documents MAESTRO_CLI_NO_ANALYTICS as the
    # opt-out. Flows must be hermetic; analytics must never gate them.
    env.setdefault("MAESTRO_CLI_NO_ANALYTICS", "1")
    env.setdefault("MAESTRO_CLI_ANALYSIS_NOTIFICATION_DISABLED", "1")

    if platform == "android":
        prefer_ipv4 = "-Djava.net.preferIPv4Stack=true"
        current = env.get("JAVA_TOOL_OPTIONS", "").strip()
        if prefer_ipv4 not in current.split():
            env["JAVA_TOOL_OPTIONS"] = f"{current} {prefer_ipv4}".strip()
    return env


def _prepare_maestro_invocation(
    *,
    session_id: str,
    platform: str,
    device_id: str,
    flow_files: list[str],
    extra_args: list[str],
) -> tuple[list[str], dict[str, str], str]:
    final_args = list(extra_args)

    debug_output = _extract_maestro_option(final_args, "--debug-output")
    if not debug_output:
        debug_output = _maestro_debug_output_dir(session_id)
        final_args.extend(["--debug-output", debug_output])

    if platform == "android":
        if not (
            _maestro_has_flag(final_args, "--reinstall-driver")
            or _maestro_has_flag(final_args, "--no-reinstall-driver")
        ):
            final_args.append("--reinstall-driver")

    cmd = ["maestro", "--device", device_id, "test"] + flow_files + final_args
    return cmd, _maestro_env(platform), debug_output


def _summarize_maestro_failure(
    *,
    session_id: str,
    platform: str,
    debug_output: str,
) -> str | None:
    debug_root = Path(debug_output)
    log_candidates: list[Path] = []
    command_log = debug_root / "simemu-command.log"
    if command_log.exists():
        log_candidates.append(command_log)
    direct_log = debug_root / "maestro.log"
    if direct_log.exists():
        log_candidates.append(direct_log)
    log_candidates.extend(debug_root.rglob("maestro.log"))
    if not log_candidates:
        return None
    log_path = max(log_candidates, key=lambda path: path.stat().st_mtime)

    try:
        log_text = log_path.read_text(errors="ignore")
    except OSError:
        return None

    if platform == "android" and _MAESTRO_ANDROID_DRIVER_BOOT_ERROR_RE.search(log_text):
        reclaim = require_session(session_id).reclaim_command()
        return (
            "Android Maestro driver failed before the flow started. "
            "The Maestro helper apps or instrumentation runner never brought up the local "
            "gRPC bridge on port 7001. simemu already forced driver reinstall and IPv4 loopback; "
            "if this still happens, treat it as a Maestro/emulator startup issue rather than an app "
            f"selector bug. Debug output: {debug_output}. Re-claim to retry: {reclaim}"
        )

    if platform == "android" and _MAESTRO_ANDROID_TRANSPORT_LOSS_RE.search(log_text):
        reclaim = require_session(session_id).reclaim_command()
        return (
            "Android Maestro lost adb/gRPC transport while the flow was running. "
            "This usually means the emulator rebooted, the adb serial disappeared, or the "
            "Maestro driver lost its forwarded connection before it could read hierarchy. "
            "simemu will re-resolve the session device and retry once; if it still fails, "
            f"treat it as simulator transport instability, not app proof. Debug output: {debug_output}. "
            f"Re-claim to retry: {reclaim}"
        )

    return None


def _run_maestro_process(cmd: list[str], env: dict[str, str]):
    """Run Maestro while preserving output for retry classification."""
    import subprocess as _sp
    import sys as _sys

    debug_output = Path(cmd[cmd.index("--debug-output") + 1])
    try:
        result = _sp.run(
            cmd,
            env=env,
            timeout=MAESTRO_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
    except TypeError:
        # Unit-test fakes often implement a small subset of subprocess.run.
        result = _sp.run(cmd, env=env, timeout=MAESTRO_TIMEOUT_SECONDS)
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    if not isinstance(stdout, str):
        stdout = ""
    if not isinstance(stderr, str):
        stderr = ""
    if stdout:
        print(stdout, end="", file=_sys.stdout)
    if stderr:
        print(stderr, end="", file=_sys.stderr)
    if stdout or stderr:
        debug_output.mkdir(parents=True, exist_ok=True)
        (debug_output / "simemu-command.log").write_text(stdout + stderr)
    return result


def _is_maestro_bridge_process(command: str) -> bool:
    lowered = command.lower()
    return (
        "maestro-driver" in lowered
        or "maestro.cli.appkt" in lowered
        or "maestro-d" in lowered
    )


def _parse_elapsed_seconds(value: str) -> int:
    value = value.strip()
    if not value:
        return 0
    days = 0
    if "-" in value:
        day_part, value = value.split("-", 1)
        try:
            days = int(day_part)
        except ValueError:
            days = 0
    parts = value.split(":")
    try:
        if len(parts) == 2:
            hours = 0
            minutes, seconds = (int(part) for part in parts)
        elif len(parts) == 3:
            hours, minutes, seconds = (int(part) for part in parts)
        else:
            return 0
    except ValueError:
        return 0
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _maestro_bridge_listener_pids(port: int) -> list[int]:
    import subprocess as _sp

    try:
        result = _sp.run(
            ["lsof", "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except Exception as exc:
        print(f"Could not inspect Maestro bridge port {port}: {exc}", file=sys.stderr)
        return []

    stdout = result.stdout if isinstance(result.stdout, str) else ""
    pids: list[int] = []
    for raw_pid in stdout.splitlines():
        raw_pid = raw_pid.strip()
        if raw_pid.isdigit():
            pids.append(int(raw_pid))
    return pids


def _process_age_and_command(pid: int) -> tuple[int, str]:
    import subprocess as _sp

    result = _sp.run(
        ["ps", "-p", str(pid), "-o", "etime=", "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    line = stdout.strip()
    if result.returncode != 0 or not line:
        return 0, ""
    parts = line.split(None, 1)
    if len(parts) == 1:
        return _parse_elapsed_seconds(parts[0]), ""
    return _parse_elapsed_seconds(parts[0]), parts[1]


def _clear_stale_maestro_host_bridge(port: int = MAESTRO_ANDROID_BRIDGE_PORT) -> list[int]:
    """Free Maestro's fixed host bridge port when a stale driver owns it.

    Android Maestro talks gRPC through localhost:7001. A previous iOS/Android
    Maestro driver can survive a failed run and keep that port bound while
    returning plain HTTP, which makes the next Android run fail with
    "First received frame was not SETTINGS" before the app flow starts.
    """
    killed: list[int] = []
    for pid in _maestro_bridge_listener_pids(port):
        age_seconds, command = _process_age_and_command(pid)
        if not command:
            continue
        if not _is_maestro_bridge_process(command):
            continue
        if age_seconds < MAESTRO_STALE_BRIDGE_SECONDS:
            raise RuntimeError(
                f"Maestro bridge port {port} is owned by an active Maestro process "
                f"(pid {pid}, age {age_seconds}s). Refusing to kill it; retry after that flow exits."
            )
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except OSError:
            continue

    if killed:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            remaining = set(_maestro_bridge_listener_pids(port)).intersection(killed)
            if not remaining:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError(
                f"Maestro bridge port {port} is still owned by stale process(es) "
                f"{', '.join(str(pid) for pid in killed)} after SIGTERM."
            )
    return killed


def _start_session_keepalive(session_id: str, interval_seconds: int = 30):
    """Keep long-running device commands from expiring mid-process."""
    stop = threading.Event()

    def keepalive() -> None:
        while not stop.wait(interval_seconds):
            now = _now_iso()
            try:
                with _locked_sessions() as (data, save):
                    raw = data.get("sessions", {}).get(session_id)
                    if not raw or raw.get("status") in ("expired", "released"):
                        return
                    raw["heartbeat_at"] = now
                    raw["status"] = "active"
                    raw["expires_at"] = _compute_expires_at("active", now)
                    save(data)
            except Exception:
                return

    thread = threading.Thread(target=keepalive, name=f"simemu-keepalive-{session_id}", daemon=True)
    thread.start()
    return stop


def _stabilize_android_maestro_driver(device_id: str) -> None:
    """Clear stale Android Maestro driver state before launching a proof flow.

    Maestro's Android bridge binds localhost:7001. When a previous run dies
    mid-startup, the next run can connect to stale/non-gRPC traffic and fail
    with "First received frame was not SETTINGS" before the app flow starts.
    """
    import subprocess as _sp

    _clear_stale_maestro_host_bridge()

    def run(args: list[str], timeout: int = 10) -> str:
        try:
            result = _sp.run(
                ["adb", "-s", device_id] + args,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except Exception:
            # Best-effort cleanup must never mask the proof command itself.
            # This also keeps unit-test subprocess fakes focused on Maestro.
            return ""
        return (result.stdout or "") + (result.stderr or "")

    try:
        forwards = _sp.run(
            ["adb", "forward", "--list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        for line in (forwards.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "tcp:7001":
                _sp.run(
                    ["adb", "-s", parts[0], "forward", "--remove", "tcp:7001"],
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
    except Exception:
        pass

    run(["forward", "--remove", "tcp:7001"], timeout=5)
    run(["forward", "--remove-all"], timeout=5)

    packages_output = run(["shell", "pm", "list", "packages"], timeout=10)
    packages = []
    for line in packages_output.splitlines():
        package = line.strip().removeprefix("package:")
        if "maestro" in package.lower():
            packages.append(package)

    packages.extend([
        "dev.mobile.maestro",
        "dev.mobile.maestro.test",
        "maestro",
        "maestro.test",
    ])

    for package in sorted(set(pkg for pkg in packages if pkg)):
        run(["shell", "am", "force-stop", package], timeout=5)
        run(["shell", "pm", "clear", package], timeout=8)


def _retry_android_maestro_bridge(
    *,
    session_id: str,
    sim_id: str,
    expected_app: str | None,
    android_kwargs: dict,
    flow_files: list[str],
    extra_args: list[str],
) -> tuple[bool, str, str | None]:
    import subprocess as _sp

    raw_session = _read_sessions_raw().get("sessions", {}).get(session_id, {})
    pinned_serial = raw_session.get("pinned_serial")
    if pinned_serial:
        android_kwargs = {**android_kwargs, "pinned_serial": pinned_serial}
    _wait_for_android_maestro_transport(sim_id, android_kwargs)
    android.dismiss_system_dialogs(sim_id, **android_kwargs)
    _restore_android_location_if_recorded(session_id, sim_id, android_kwargs)
    _verify_or_repair_android_maestro_target(
        session_id=session_id,
        sim_id=sim_id,
        expected_app=expected_app,
        android_kwargs=android_kwargs,
    )
    _ensure_maestro_target_foreground(
        session_id=session_id,
        platform="android",
        sim_id=sim_id,
        expected_app=expected_app,
        android_kwargs=android_kwargs,
    )
    time.sleep(2.0)

    device_id = android._serial(sim_id, pinned=android_kwargs.get("pinned_serial"))
    _stabilize_android_maestro_driver(device_id)
    cmd, env, retry_debug_output = _prepare_maestro_invocation(
        session_id=session_id,
        platform="android",
        device_id=device_id,
        flow_files=flow_files,
        extra_args=extra_args,
    )
    retry_result = _run_maestro_process(cmd, env)
    return retry_result.returncode == 0, retry_debug_output, _summarize_maestro_failure(
        session_id=session_id,
        platform="android",
        debug_output=retry_debug_output,
    )


def _preflight_android_maestro_device(
    *,
    session_id: str,
    sim_id: str,
    android_kwargs: dict,
) -> tuple[str, dict]:
    """Wait for an Android session to be adb/pm-ready before Maestro starts.

    Resolving a serial is not enough for Maestro: the driver install and gRPC
    bridge setup also need a responsive package manager. Fail before starting
    Maestro if the emulator is still booting or adb transport is unstable.
    """
    session = touch(session_id)
    if session.pinned_serial:
        android_kwargs = {**android_kwargs, "pinned_serial": session.pinned_serial}
    serial = _wait_for_android_maestro_transport(sim_id, android_kwargs)
    if android_kwargs.get("pinned_serial") != serial:
        android_kwargs = {**android_kwargs, "pinned_serial": serial}
    android.dismiss_system_dialogs(sim_id, **android_kwargs)
    _restore_android_location_if_recorded(session_id, sim_id, android_kwargs)
    return serial, android_kwargs


def _wait_for_android_maestro_transport(sim_id: str, android_kwargs: dict) -> str:
    timeout = int(os.environ.get("SIMEMU_ANDROID_MAESTRO_TRANSPORT_TIMEOUT", "120"))
    try:
        return android.wait_until_ready(sim_id, timeout=timeout, **android_kwargs)
    except RuntimeError as exc:
        detail = str(exc).lower()
        if not (
            "not connected" in detail
            or "offline" in detail
            or "device not found" in detail
            or "adb-ready" in detail
        ):
            raise
        return android.wait_for_transport_recovery(sim_id, timeout=timeout, **android_kwargs)


def _restore_android_location_if_recorded(session_id: str, sim_id: str, android_kwargs: dict) -> None:
    provenance = get_provenance(session_id)
    location_state = provenance.get("last_location")
    if not isinstance(location_state, dict):
        return
    try:
        lat = float(location_state["lat"])
        lng = float(location_state["lng"])
    except (KeyError, TypeError, ValueError):
        return
    try:
        android.verify_location(sim_id, lat, lng, timeout=2, **android_kwargs)
    except RuntimeError:
        android.location(sim_id, lat, lng, **android_kwargs)


def _compute_expires_at(status: str, heartbeat_at: str) -> str:
    """Compute when a session will next transition based on current status."""
    from datetime import timedelta
    hb = datetime.fromisoformat(heartbeat_at)
    if status == "active":
        return (hb + timedelta(seconds=IDLE_TIMEOUT)).isoformat()
    elif status == "idle":
        return (hb + timedelta(seconds=IDLE_TIMEOUT + PARK_TIMEOUT)).isoformat()
    elif status == "parked":
        return (hb + timedelta(seconds=EXPIRE_TIMEOUT)).isoformat()
    return heartbeat_at


# ── state persistence ────────────────────────────────────────────────────────

SCHEMA_VERSION = 4  # Current schema version


def _sessions_file() -> Path:
    return state.state_dir() / "sessions.json"


def _sessions_lock_file() -> Path:
    return state.state_dir() / "sessions.lock"


def _android_maestro_lock_file() -> Path:
    return state.state_dir() / "android-maestro.lock"


def _migrate_schema(data: dict) -> dict:
    """Migrate sessions.json to the current schema version.

    Migrations are additive — they add fields with defaults, never remove.
    """
    version = data.get("schema_version", 1)

    if version < 2:
        # v1 → v2: Add provenance dict and claim spec fields to each session
        for sid, session in data.get("sessions", {}).items():
            session.setdefault("provenance", {})
            session.setdefault("claim_platform", session.get("platform", ""))
            session.setdefault("claim_form_factor", session.get("form_factor", "phone"))
            session.setdefault("claim_os_version", session.get("os_version"))
            session.setdefault("claim_real_device", session.get("real_device", False))
            session.setdefault("claim_label", session.get("label", ""))
            session.setdefault("resolved_os_version", None)
            session.setdefault("last_build_artifact", None)
        data["schema_version"] = 2

    if version < 3:
        # v2 → v3: Add pinned_serial for Android session isolation
        for sid, session in data.get("sessions", {}).items():
            session.setdefault("pinned_serial", None)
        data["schema_version"] = 3

    if version < 4:
        # v3 → v4: Add exclusive-claim ownership fields
        for sid, session in data.get("sessions", {}).items():
            session.setdefault("pid", None)
            session.setdefault("claim_pgid", None)
            session.setdefault("claim_token", None)
        data["schema_version"] = 4

    return data


@contextmanager
def _locked_sessions():
    base = state.state_dir()
    base.mkdir(parents=True, exist_ok=True)
    lock_fd = open(_sessions_lock_file(), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data = _read_sessions_raw()
        pending: list[dict] = []

        def save(new_data: dict):
            pending.append(new_data)

        yield data, save

        if pending:
            _write_sessions_raw(pending[-1])
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


@contextmanager
def _locked_android_maestro():
    """Serialize Maestro runs that can contend for the fixed host bridge port."""
    base = state.state_dir()
    base.mkdir(parents=True, exist_ok=True)
    lock_fd = open(_android_maestro_lock_file(), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _read_sessions_raw() -> dict:
    sf = _sessions_file()
    bak = sf.with_suffix(".bak")

    # Try primary file
    if sf.exists():
        try:
            data = json.loads(sf.read_text())
            if isinstance(data, dict) and "sessions" in data:
                if data.get("schema_version", 1) < SCHEMA_VERSION:
                    data = _migrate_schema(data)
                    try:
                        _write_sessions_raw(data)
                    except OSError:
                        pass
                return data
        except (json.JSONDecodeError, OSError):
            pass

    # Primary corrupted or missing — try backup
    if bak.exists():
        try:
            data = json.loads(bak.read_text())
            if isinstance(data, dict) and "sessions" in data:
                # Restore from backup
                import sys as _sys
                print(json.dumps({
                    "diagnostic": "sessions_recovered_from_backup",
                    "primary": str(sf),
                    "backup": str(bak),
                }), file=_sys.stderr, flush=True)
                # Write it back as primary
                try:
                    sf.write_text(json.dumps(data, indent=2))
                except OSError:
                    pass
                return data
        except (json.JSONDecodeError, OSError):
            pass

    # NB: do NOT clean up `.tmp` here. `_read_sessions_raw` is called from
    # lock-free paths (e.g. `get_active_sessions`), so deleting a `.tmp` that
    # belongs to a concurrent in-flight writer would cause its
    # `tmp.replace(sf)` to fail with FileNotFoundError. Stale `.tmp` files from
    # crashed writes are harmless — the next write recreates and renames them.

    return {"sessions": {}}


def _write_sessions_raw(data: dict):
    sf = _sessions_file()
    bak = sf.with_suffix(".bak")
    tmp = sf.with_suffix(".tmp")

    # Stamp schema version
    data["schema_version"] = SCHEMA_VERSION

    content = json.dumps(data, indent=2)

    # Validate JSON roundtrips before writing
    try:
        json.loads(content)
    except json.JSONDecodeError:
        raise RuntimeError("BUG: attempted to write invalid JSON to sessions.json")

    # Create backup of current file before overwriting
    if sf.exists():
        try:
            import shutil
            shutil.copy2(sf, bak)
        except OSError:
            pass

    # Atomic write: tmp → rename. `write_text` overwrites any pre-existing
    # .tmp (e.g. one left by a previously-crashed writer), so stale .tmp files
    # self-heal here. We deliberately do NOT clean up .tmp in the lock-free
    # read path — doing so races with concurrent in-flight writers and causes
    # FileNotFoundError on their replace().
    tmp.write_text(content)
    tmp.replace(sf)


def _session_from_dict(d: dict) -> Session:
    # Handle any extra keys gracefully
    known_fields = {f.name for f in Session.__dataclass_fields__.values()}
    filtered = {k: v for k, v in d.items() if k in known_fields}
    return Session(**filtered)


def _session_log(message: str) -> None:
    """Emit session diagnostics to stderr so JSON stdout stays machine-readable."""
    print(message, file=sys.stderr, flush=True)


# ── public API ───────────────────────────────────────────────────────────────

def _reap_dead_claims_locked(
    data: dict, command_lines: list[str] | None = None
) -> list[str]:
    """Mark reapable sessions as expired. Returns reaped IDs.

    A session is reapable only when its claimant PID is dead AND no running
    process still references its device — a dead claimant with a live
    `xcodebuild` on its UDID is left alone.

    `command_lines`, when given, is a pre-fetched process-table snapshot to
    reuse instead of probing again — see `_claim_once`, which probes once
    and threads the same snapshot through both this and the earlier
    unlocked `reap_dead_claims()` call, rather than each probing separately.
    """
    stale = exclusive.collect_stale_session_ids(data.get("sessions", {}), command_lines)
    if stale:
        exclusive.mark_sessions_reaped(data, stale, _now_iso())
        for sid in stale:
            _session_log(
                f"[simemu-session] Reaped session '{sid}' — claimant PID dead, device idle"
            )
    return stale


def reap_dead_claims(command_lines: list[str] | None = None) -> list[str]:
    """Public: reap sessions whose claimant PID is no longer alive.

    Pass `command_lines` to reuse an already-fetched process-table snapshot
    (see `_claim_once`); omitted, this probes internally.
    """
    with _locked_sessions() as (data, save):
        reaped = _reap_dead_claims_locked(data, command_lines)
        if reaped:
            save(data)
        return reaped


def claim(spec: ClaimSpec, wait_seconds: int = 0) -> Session:
    """Find the best available device matching spec, boot it, and return a session.

    If `wait_seconds > 0` and no device is available (pool exhausted or every
    candidate just got claimed by a sibling agent), retry on a short interval
    until either a device becomes free or the budget is exhausted.
    """
    state.check_maintenance()

    deadline = time.monotonic() + max(0, wait_seconds)
    last_err: Exception | None = None
    while True:
        try:
            return _claim_once(spec)
        except (NoSimulatorAvailable, SessionError) as exc:
            # SessionError("device_already_claimed") and NoSimulatorAvailable
            # are the two race conditions worth retrying. Everything else
            # propagates immediately.
            if isinstance(exc, SessionError) and exc.error_type != "device_already_claimed":
                raise
            last_err = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(1.0, remaining))


def _claim_once(spec: ClaimSpec) -> Session:
    """Single-shot claim attempt. Use claim() for the public wait-aware wrapper."""
    # Probe the process table once per attempt and reuse the same snapshot for
    # both reap call sites below (here, and again under the session lock further
    # down). Previously each site probed separately (T-LU-054 finding 3) — besides
    # the duplicate work, two probes taken moments apart can disagree, and each
    # extra probe widens the window between "device confirmed idle" and "claim
    # marked expired" (finding 2). One snapshot, reused, closes both gaps as far
    # as they can be closed without a broader locking change.
    #
    # If the probe itself fails we get None back (see running_process_command_lines)
    # and skip reaping entirely this attempt rather than reap under unknown device
    # state — finding 1, fail closed.
    reap_command_lines = exclusive.running_process_command_lines()
    if reap_command_lines is not None:
        reap_dead_claims(reap_command_lines)

    agent = os.environ.get("SIMEMU_AGENT") or f"pid-{os.getpid()}"
    now = _now_iso()

    # T-31: Rate limiting
    _check_rate_limit(agent)

    # T-26: Progress feedback
    import sys as _sys
    print(f"Claiming {spec.platform} {spec.form_factor}...", file=_sys.stderr, flush=True)

    # Generate session ID
    session_id = _gen_session_id()

    if spec.platform == "macos":
        # macOS apps run natively — no simulator/emulator needed
        mac_ver = _platform_mod.mac_ver()[0] or "unknown"
        session = Session(
            session_id=session_id,
            platform="macos",
            form_factor="desktop",
            os_version=spec.os_version,
            real_device=True,
            label=spec.label,
            status="active",
            sim_id="macos-native",
            device_name=mac_ver,
            agent=agent,
            created_at=now,
            heartbeat_at=now,
            resolved_os_version=mac_ver,
            claim_platform="macos",
            claim_form_factor="desktop",
            claim_os_version=spec.os_version,
            claim_real_device=True,
            claim_device_selector=spec.device_selector,
            claim_label=spec.label,
            pid=exclusive.claimant_pid(),
            claim_pgid=_safe_pgid(),
            claim_token=exclusive.issue_claim_token(),
        )
        session.expires_at = _compute_expires_at("active", now)

        with _locked_sessions() as (data, save):
            # Allow multiple macOS sessions (different apps on same machine)
            data["sessions"][session_id] = asdict(session)
            save(data)

        return session

    # Check memory budget before claiming
    _enforce_memory_budget_if_needed(spec.platform)

    # Reconcile stale Android session pins before selecting a new device.
    # This prevents fresh claims from reusing emulator ports still "owned" by
    # dead or parked sessions in the session store.
    _reconcile_android_sessions()

    # Find best matching device
    sim = find_best_device(spec)
    pinned_serial: str | None = None

    def _prepare_android_candidate(candidate):
        """Return a usable adb serial for a candidate, recovering broken booted AVDs."""
        if candidate.real_device:
            return None

        attempted_recovery = False
        while True:
            if not candidate.booted or attempted_recovery:
                print(f"Booting {candidate.device_name}...", file=_sys.stderr, flush=True)
                android.boot(candidate.sim_id, headless=not spec.visible)

            try:
                serial = android.wait_until_ready(candidate.sim_id, timeout=45)
                print("Ready.", file=_sys.stderr, flush=True)
                return serial
            except RuntimeError as exc:
                if attempted_recovery:
                    raise RuntimeError(str(exc))
                attempted_recovery = True
                print(
                    f"Recovering {candidate.device_name} after adb-ready failure...",
                    file=_sys.stderr,
                    flush=True,
                )
                try:
                    android.shutdown(candidate.sim_id)
                except RuntimeError:
                    pass

    # Boot the device if not already booted and not a real device.
    # Android gets extra resilience: validate adb readiness even for already-
    # booted emulators, recover once, then fall back to the next candidate.
    if sim.platform == "android" and not sim.real_device:
        prep_errors = []
        candidates = [sim] + [
            candidate for candidate in find_matching_devices(spec)
            if candidate.sim_id != sim.sim_id
        ]
        for candidate in candidates:
            try:
                pinned_serial = _prepare_android_candidate(candidate)
                sim = candidate
                break
            except RuntimeError as exc:
                prep_errors.append(f"{candidate.sim_id}: {exc}")
                continue
        else:
            raise RuntimeError(
                "No Android emulator could be prepared for this claim.\n" +
                "\n".join(prep_errors)
            )
    elif not sim.real_device and not sim.booted:
        print(f"Booting {sim.device_name}...", file=_sys.stderr, flush=True)
        if sim.platform in ("ios", "watchos", "tvos", "visionos"):
            ios.boot(sim.sim_id)
        else:
            android.boot(sim.sim_id, headless=not spec.visible)
        print(f"Ready.", file=_sys.stderr, flush=True)

    # Apply window management — headless by default unless --visible
    if not sim.real_device and not spec.visible:
        try:
            window_mgr.apply_window_mode(sim.sim_id, sim.platform, sim.device_name)
        except Exception:
            pass  # window management is best-effort

    # Pin the adb serial at claim time for Android session isolation
    if spec.platform == "android" and not sim.real_device:
        pinned_serial = pinned_serial or android.get_android_serial(sim.sim_id, retries=4, delay=1.0)

    # Create the session
    session = Session(
        session_id=session_id,
        platform=spec.platform,
        form_factor=spec.form_factor,
        os_version=spec.os_version,
        real_device=sim.real_device,
        label=spec.label,
        status="active",
        sim_id=sim.sim_id,
        device_name=sim.device_name,
        agent=agent,
        created_at=now,
        heartbeat_at=now,
        resolved_os_version=sim.runtime,
        pinned_serial=pinned_serial,
        claim_platform=spec.platform,
        claim_form_factor=spec.form_factor,
        claim_os_version=spec.os_version,
        claim_real_device=spec.real_device,
        claim_device_selector=spec.device_selector,
        claim_label=spec.label,
        pid=exclusive.claimant_pid(),
        claim_pgid=_safe_pgid(),
        claim_token=exclusive.issue_claim_token(),
    )
    session.expires_at = _compute_expires_at("active", now)

    # Persist session — check for duplicate sim_id under lock
    with _locked_sessions() as (data, save):
        # Reap any dead-PID claimants before evaluating contention. Reuses the
        # single process-table snapshot taken at the top of this attempt (see
        # there for why) rather than probing again.
        if reap_command_lines is not None:
            _reap_dead_claims_locked(data, reap_command_lines)

        # Check if another active session already has this device
        for existing_id, existing in list(data["sessions"].items()):
            if (existing.get("sim_id") == sim.sim_id
                    and existing.get("status") in ("active", "idle", "parked")):
                existing_session = _session_from_dict(existing)
                if _is_effectively_expired(existing_session):
                    data["sessions"][existing_id]["status"] = "expired"
                    data["sessions"][existing_id]["expires_at"] = _now_iso()
                    _session_log(
                        f"[simemu-session] Expired timed-out session '{existing_id}' "
                        f"(device '{sim.sim_id}') to allow new claim"
                    )
                    continue

                # Is the existing session's emulator actually running?
                if existing.get("platform") == "android" and not existing.get("real_device"):
                    existing_serial = android.get_android_serial(sim.sim_id, retries=1, delay=0.3)
                    if existing_serial is None:
                        # Emulator is dead — expire the zombie session
                        data["sessions"][existing_id]["status"] = "expired"
                        data["sessions"][existing_id]["expires_at"] = _now_iso()
                        _session_log(
                            f"[simemu-session] Expired zombie session '{existing_id}' "
                            f"(emulator '{sim.sim_id}' not running) to allow new claim"
                        )
                        continue
                raise SessionError(
                    error="device_already_claimed",
                    session=None,
                    hint=f"Device '{sim.device_name}' is already claimed by session {existing_id}. "
                         f"Release it first: simemu do {existing_id} done",
                    existing_session=existing_id,
                )
        data["sessions"][session_id] = asdict(session)
        save(data)

    return session


def get_session(session_id: str) -> Session | None:
    """Return a session by ID, or None if not found."""
    data = _read_sessions_raw()
    raw = data["sessions"].get(session_id)
    if raw is None:
        return None
    return _session_from_dict(raw)


def _is_effectively_expired(session: Session) -> bool:
    """Return True when a session's terminal expiry deadline has passed.

    We still run a lifecycle monitor, but session reads must be defensive in
    case the monitor/daemon is delayed or started with a bad environment.
    ``expires_at`` is the next lifecycle transition deadline for active/idle
    sessions, not terminal expiry; long-running commands may cross it before
    they can refresh heartbeat.
    """
    if session.status in ("expired", "released"):
        return session.status == "expired"
    if not session.heartbeat_at:
        return False
    try:
        heartbeat = datetime.fromisoformat(session.heartbeat_at)
    except ValueError:
        return False
    from datetime import timedelta
    return datetime.now(timezone.utc) >= heartbeat + timedelta(seconds=EXPIRE_TIMEOUT)


def _mark_expired(session_id: str) -> None:
    with _locked_sessions() as (data, save):
        if session_id in data["sessions"] and data["sessions"][session_id].get("status") not in ("expired", "released"):
            data["sessions"][session_id]["status"] = "expired"
            save(data)


def _android_disconnect_grace_elapsed(raw: dict) -> bool:
    """Return True once an Android session has been unreachable long enough to expire."""
    stamp = raw.get("heartbeat_at") or raw.get("created_at")
    if not stamp:
        return True
    try:
        last_seen = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last_seen).total_seconds() >= ANDROID_DISCONNECT_GRACE


def _reconcile_android_sessions() -> list[str]:
    """Persist Android session cleanup outside the daemon path."""
    with _locked_sessions() as (data, save):
        changed = _reconcile_android_sessions_locked(data)
        if changed:
            save(data)
        return changed


def _reconcile_android_sessions_locked(data: dict) -> list[str]:
    """Normalize Android session pins while holding the session lock.

    Rules:
    - parked Android emulator sessions intentionally have no running device,
      so their pinned adb serial must be cleared
    - active/idle Android emulator sessions must either:
      - keep a pin that still validates for their AVD, or
      - re-pin to the current serial for that AVD, or
      - expire if no emulator for that AVD is actually running
    """
    changed: list[str] = []

    for sid, raw in list(data["sessions"].items()):
        if raw.get("status") in ("expired", "released"):
            continue
        if raw.get("platform") != "android" or raw.get("real_device"):
            continue

        status = raw.get("status")
        sim_id = raw.get("sim_id", "")
        pinned = raw.get("pinned_serial")

        if status == "parked":
            if pinned is not None:
                data["sessions"][sid]["pinned_serial"] = None
                changed.append(sid)
            continue

        if status not in ("active", "idle"):
            continue

        if pinned and android.validate_serial(pinned, sim_id):
            continue

        serial = android.get_android_serial(sim_id, retries=1, delay=0.3)
        if serial is not None:
            if serial != pinned:
                data["sessions"][sid]["pinned_serial"] = serial
                changed.append(sid)
            continue

        if not _android_disconnect_grace_elapsed(raw):
            continue

        old_status = raw.get("status")
        data["sessions"][sid]["status"] = "expired"
        data["sessions"][sid]["expires_at"] = _now_iso()
        data["sessions"][sid]["pinned_serial"] = None
        changed.append(sid)
        _session_log(
            f"[simemu-session] '{sid}' {old_status} → expired "
            f"(stale Android session: emulator '{sim_id}' not running)"
        )

    return changed


def require_session(session_id: str) -> Session:
    """Return a session by ID, or raise with actionable error."""
    session = get_session(session_id)
    if session is None:
        raise SessionError(
            error="session_not_found",
            session=session_id,
            hint=f"Session '{session_id}' does not exist. Claim a new device with: simemu claim <platform>",
        )
    if _is_effectively_expired(session):
        _mark_expired(session_id)
        raise SessionError(
            error="session_expired",
            session=session_id,
            hint=f"Session expired after inactivity. Re-claim with: {session.reclaim_command()}",
            expired_at=session.expires_at,
        )
    if session.status == "expired":
        raise SessionError(
            error="session_expired",
            session=session_id,
            hint=f"Session expired after inactivity. Re-claim with: {session.reclaim_command()}",
            expired_at=session.expires_at,
        )
    if session.status == "released":
        raise SessionError(
            error="session_released",
            session=session_id,
            hint=f"Session was released. Re-claim with: {session.reclaim_command()}",
        )

    # Optional token validation — enabled by setting SIMEMU_SESSION_TOKEN in
    # the env. When unset, ownership is trust-on-first-use (preserves the
    # existing CLI contract: agents that just received a session id can use it
    # without juggling secrets). When set, the value MUST match the token
    # issued at claim time. Sessions created before this feature have no
    # token and are exempted (legacy compat).
    presented = os.environ.get("SIMEMU_SESSION_TOKEN")
    if presented and session.claim_token and not exclusive.validate_token(
        session.claim_token, presented
    ):
        raise SessionError(
            error="session_token_mismatch",
            session=session_id,
            hint="SIMEMU_SESSION_TOKEN does not match the token issued at claim time.",
        )

    return session


def touch(session_id: str) -> Session:
    """Update heartbeat and ensure session is active. Re-boots parked devices."""
    session = require_session(session_id)
    now = _now_iso()

    reboot_needed = session.status == "parked"

    # macOS sessions are native — never need rebooting
    if session.platform == "macos":
        reboot_needed = False

    # Android session claims can occasionally outlive the underlying emulator
    # process, or the adb connection can go temporarily offline after an
    # activity-alias switch (app restart). Try to recover before giving up.
    if (
        not reboot_needed
        and not session.real_device
        and session.platform == "android"
    ):
        serial = android.get_android_serial(session.sim_id, retries=2, delay=0.5)
        if serial is None:
            # adb offline — try reconnecting THIS device's serial only
            # (not `adb reconnect offline` which reconnects ALL devices and causes
            # cross-session contamination when multiple emulators are running)
            try:
                import subprocess as _sp
                if session.pinned_serial:
                    # Reconnect only the pinned serial
                    _sp.run(["adb", "-s", session.pinned_serial, "reconnect"],
                            capture_output=True, timeout=10, check=False)
                import time as _time
                _time.sleep(2)
                serial = android.get_android_serial(session.sim_id, retries=4, delay=1.0)
            except Exception:
                serial = None
            if serial is None and os.environ.get("SIMEMU_DISABLE_SESSION_AUTO_REBOOT") == "1":
                # Continuous proof commands must not reboot, but adb can briefly lose
                # a just-captured emulator. Give the original app session a chance to
                # reappear before reporting that a reboot would be required.
                serial = android.get_android_serial(session.sim_id, retries=10, delay=1.0)
                if serial is None and session.pinned_serial:
                    # AVD-name lookup can fail transiently even while the pinned adb
                    # transport still exists. In no-reboot proof mode, keep using the
                    # pinned serial and let the command-level adb call return a normal
                    # failure if the transport is truly gone.
                    serial = session.pinned_serial
            if serial is not None and session.pinned_serial and serial != session.pinned_serial:
                # Serial changed after reconnect — update pinned serial
                with _locked_sessions() as (data, save):
                    if session_id in data["sessions"]:
                        data["sessions"][session_id]["pinned_serial"] = serial
                        save(data)
                session.pinned_serial = serial
            if serial is None:
                reboot_needed = True
        if serial is not None and not session.pinned_serial:
            with _locked_sessions() as (data, save):
                if session_id in data["sessions"]:
                    data["sessions"][session_id]["pinned_serial"] = serial
                    save(data)
            session.pinned_serial = serial
        if serial is not None and session.pinned_serial and session.pinned_serial != serial:
            # The emulator can restart on a new adb port while keeping the same
            # AVD name. Keep the session pin current so Maestro and screenshots
            # do not target a dead serial after recovery.
            with _locked_sessions() as (data, save):
                if session_id in data["sessions"]:
                    data["sessions"][session_id]["pinned_serial"] = serial
                    save(data)
            session.pinned_serial = serial

    # Real device connectivity check — verify the device is still connected
    if session.real_device:
        if session.platform in ("ios", "watchos", "tvos", "visionos"):
            # iOS real device — check via devicectl
            try:
                from .discover import list_real_ios
                connected = any(d.sim_id == session.sim_id for d in list_real_ios(set()))
                if not connected:
                    raise RuntimeError(
                        f"Real iOS device '{session.device_name}' is no longer connected.\n"
                        f"Reconnect the device and retry, or re-claim: {session.reclaim_command()}"
                    )
            except ImportError:
                pass
        elif session.platform == "android":
            # Android real device — check adb
            from . import device as real_device
            connected = any(d.device_id == session.sim_id for d in real_device.list_android_devices())
            if not connected:
                raise RuntimeError(
                    f"Real Android device '{session.device_name}' is no longer connected via adb.\n"
                    f"Reconnect and retry, or re-claim: {session.reclaim_command()}"
                )

    # Re-boot if parked (simulators only — real devices don't boot)
    if reboot_needed:
        if os.environ.get("SIMEMU_DISABLE_SESSION_AUTO_REBOOT") == "1":
            raise RuntimeError(
                f"Session '{session_id}' would auto-reboot '{session.device_name}', "
                "but SIMEMU_DISABLE_SESSION_AUTO_REBOOT=1 is set."
            )
        if not session.real_device:
            if session.platform in ("ios", "watchos", "tvos", "visionos"):
                ios.boot(session.sim_id)
            else:
                session.pinned_serial = None
                android.boot(session.sim_id, headless=True)
                # Re-pin serial after boot — emulator gets a new port
                new_serial = android.get_android_serial(session.sim_id, retries=6, delay=1.0)
                if new_serial and new_serial != session.pinned_serial:
                    session.pinned_serial = new_serial

    # Update state
    with _locked_sessions() as (data, save):
        if session_id in data["sessions"]:
            data["sessions"][session_id]["heartbeat_at"] = now
            data["sessions"][session_id]["status"] = "active"
            data["sessions"][session_id]["expires_at"] = _compute_expires_at("active", now)
            # Persist updated pinned serial
            if session.pinned_serial:
                data["sessions"][session_id]["pinned_serial"] = session.pinned_serial
            save(data)

    session.heartbeat_at = now
    session.status = "active"
    session.expires_at = _compute_expires_at("active", now)
    return session


def renew(session_id: str, hours: float | None = None) -> Session:
    """Proactively extend a session before it expires."""
    session = require_session(session_id)
    now = _now_iso()

    with _locked_sessions() as (data, save):
        if session_id in data["sessions"]:
            data["sessions"][session_id]["heartbeat_at"] = now
            data["sessions"][session_id]["status"] = "active"
            data["sessions"][session_id]["expires_at"] = _compute_expires_at("active", now)
            save(data)

    session.heartbeat_at = now
    session.status = "active"
    session.expires_at = _compute_expires_at("active", now)
    return session


def release(session_id: str) -> Session:
    """Immediately release a session and free the device."""
    session = require_session(session_id)
    released_at = _now_iso()
    released_by = os.environ.get("SIMEMU_AGENT") or f"pid-{os.getpid()}"

    if not session.real_device:
        try:
            if session.platform in ("ios", "watchos", "tvos", "visionos"):
                ios.erase(session.sim_id)
            else:
                android.erase(session.sim_id)
        except Exception:
            try:
                if session.platform in ("ios", "watchos", "tvos", "visionos"):
                    ios.shutdown(session.sim_id)
                else:
                    android.shutdown(session.sim_id)
            except Exception:
                pass

        try:
            window_mgr.apply_window_mode(session.sim_id, session.platform, session.device_name)
        except Exception:
            pass

    with _locked_sessions() as (data, save):
        if session_id in data["sessions"]:
            data["sessions"][session_id]["status"] = "released"
            data["sessions"][session_id]["visible"] = False
            data["sessions"][session_id]["released_at"] = released_at
            data["sessions"][session_id]["released_by"] = released_by
            data["sessions"][session_id].pop("last_build_artifact", None)
            save(data)

    session.status = "released"
    return session


def get_all_sessions() -> dict[str, Session]:
    """Return all sessions (including expired/released for history)."""
    data = _read_sessions_raw()
    return {
        sid: _session_from_dict(raw)
        for sid, raw in data["sessions"].items()
    }


def get_active_sessions() -> dict[str, Session]:
    """Return only active/idle/parked sessions."""
    _reconcile_android_sessions()
    return {
        sid: s for sid, s in get_all_sessions().items()
        if s.status in ("active", "idle", "parked") and not _is_effectively_expired(s)
    }


# ── do command dispatch ──────────────────────────────────────────────────────

_COMMAND_HELP: dict[str, str] = {
    # Session lifecycle
    "boot":             "Wake a parked session and re-boot the device",
    "show":             "Make the simulator window visible",
    "hide":             "Hide the simulator window (headless)",
    "renew":            "Extend session before it expires",
    "done":             "Release the session and free the device",
    "reboot":           "Restart the simulator",
    "present":          "Present the iOS simulator window in a canonical position",
    "stabilize":        "Stabilize the iOS simulator window for reliable interaction",
    # App management
    "install":          "Install .app/.ipa (iOS) or .apk (Android)",
    "launch":           "Launch app by bundle ID or package name",
    "terminate":        "Force-stop a running app",
    "uninstall":        "Remove an installed app",
    "reset-app":        "Terminate + uninstall + reinstall + launch",
    "clear-data":       "Clear app data (Android) or hint to reinstall (iOS)",
    "clean-retry":      "Clear Android app data and relaunch from a clean state",
    "grant-all":        "Grant ALL permissions preemptively",
    "app-info":         "Show app version, data size, container path",
    "verify-install":   "Verify Android package registration after install",
    "repair-install":   "Cold-repair Android package-manager state and reinstall",
    "app-container":    "Get the app's data container path",
    "is-running":       "Check if an app is running (returns bool + pid)",
    "foreground-app":   "Return which app is currently in foreground",
    # UI interaction
    "a11y-tap":         "Tap element by accessibility label — HEADLESS via Maestro",
    "tap":              "Tap at x y coordinates (needs visible window on iOS)",
    "swipe":            "Swipe from x1 y1 to x2 y2",
    "long-press":       "Long-press at x y coordinates",
    "scroll":           "Scroll up/down/left/right",
    "back":             "Go back (edge swipe iOS, back button Android)",
    "home":             "Go to home screen",
    "key":              "Press a hardware key (home, lock, volume, etc.)",
    "software-keyboard": "Toggle iOS Simulator software keyboard visibility",
    "input":            "Type text into focused field",
    "type-submit":      "Type text and press Enter",
    "shake":            "Shake gesture (opens React Native dev menu)",
    # Capture & proof
    "screenshot":       "Take a screenshot (-o path, --max-size px)",
    "deeplink-proof":   "Open URL + wait 3s + screenshot in one command",
    "proof":            "Normalize device state + capture verified screenshot with metadata",
    "wait-for-render":  "Wait N seconds then screenshot",
    "video-start":      "Start screen recording (-o path)",
    "video-stop":       "Stop screen recording (pass pid from video-start)",
    "log-crash":        "Get recent crash logs",
    # Navigation
    "url":              "Open a URL or deep link",
    "maestro":          "Run a Maestro flow YAML",
    # Alerts & permissions
    "dismiss-alert":    "Dismiss any visible system alert",
    "accept-alert":     "Tap Allow/OK on a system alert",
    "deny-alert":       "Tap Don't Allow/Cancel on a system alert",
    "auto-dismiss":     "Accept pending alerts (focuses window first)",
    "allow-dialog":     "Tap Allow (or custom label) in a permission dialog — focuses window first",
    "deny-dialog":      "Tap Don't Allow (or custom label) in a permission dialog",
    "permission-grant": "Pre-authorize a permission before launch: <bundle> <service>",
    "launch-with-args": "Launch app with custom arguments: <bundle> [arg1 arg2 ...]",
    # Device state
    "appearance":       "Set light or dark mode",
    "rotate":           "Set orientation (portrait, landscape, left, right)",
    "location":         "Set GPS coordinates (lat lng)",
    "status-bar":       "Override status bar (--time, --battery, --wifi, --clear)",
    "biometrics":       "Simulate Face ID / fingerprint (match or fail)",
    "network":          "Set network mode: offline/slow/normal (Android)",
    # Clipboard
    "clipboard-set":    "Copy text to device clipboard",
    "clipboard-get":    "Read device clipboard (iOS only)",
    # Files
    "push":             "Push file to Android emulator",
    "pull":             "Pull file from Android emulator",
    "add-media":        "Add photo/video to device library",
    "contacts-import":  "Import contacts from VCF file",
    # System
    "keychain-reset":   "Clear iOS keychain",
    "icloud-sync":      "Trigger iCloud sync (iOS only)",
    "clone":            "Clone an iOS simulator",
    "pair":             "Pair a watchOS and iOS simulator session/device and activate the pair",
    "font-size":        "Set accessibility font size (Android)",
    "reduce-motion":    "Toggle reduce motion / animations (Android)",
    "notifications-clear": "Clear notification center (Android)",
    "a11y-tree":        "Dump accessibility hierarchy (Android)",
    # tvOS Siri Remote
    "focus-move":       "Move focus: up, down, left, right (tvOS only)",
    "focus-select":     "Press select / click trackpad (tvOS only)",
    "remote":           "Press Siri Remote button: up/down/left/right/select/menu/play-pause/home (tvOS only)",
    # Info
    "env":              "Show device info (UDID, serial, OS version)",
    "trace":            "Export debug trace bundle (session + health + history)",
    "help":             "Show this help",
}


# ── macOS native command dispatch ────────────────────────────────────────────

def _do_macos_command(session: Session, command: str, args: list[str]) -> dict:
    """Handle commands for macOS native sessions (no simulator)."""
    import subprocess as _sp

    if command == "install":
        if not args:
            raise RuntimeError("Usage: simemu do <session> install <path-to-.app>")
        app_path = args[0]
        # Copy .app bundle to staging area, or open it directly
        if app_path.endswith(".app"):
            staging = Path(tempfile.gettempdir()) / f"simemu-apps-{os.getuid()}"
            staging.mkdir(parents=True, exist_ok=True)
            _sp.run(["cp", "-R", app_path, str(staging)], check=True)
            return {"status": "installed", "app": app_path, "location": str(staging / Path(app_path).name)}
        else:
            _sp.run(["open", app_path], check=True)
            return {"status": "installed", "app": app_path}

    elif command == "launch":
        if not args:
            raise RuntimeError("Usage: simemu do <session> launch <bundle-id>")
        bundle_id = args[0]
        _sp.run(["open", "-b", bundle_id], check=True)
        return {"status": "launched", "app": bundle_id}

    elif command == "screenshot":
        output = None
        i = 0
        while i < len(args):
            if args[i] in ("-o", "--output") and i + 1 < len(args):
                output = args[i + 1]
                i += 2
            else:
                i += 1
        if not output:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = Path(os.environ.get("SIMEMU_OUTPUT_DIR", Path.home() / ".simemu"))
            out_dir.mkdir(parents=True, exist_ok=True)
            output = str(out_dir / f"{session.session_id}_{ts}.png")

        # Try to capture a specific window by bundle ID, fall back to full screen
        if args and not args[0].startswith("-"):
            bundle_id = args[0]
            # Get window ID via osascript
            result = _sp.run([
                "osascript", "-e",
                f'tell application "System Events" to tell process (name of first application process '
                f'whose bundle identifier is "{bundle_id}") to set wid to id of first window\n'
                f'return wid',
            ], capture_output=True, text=True, check=False)
            window_id = result.stdout.strip()
            if window_id and window_id.isdigit():
                _sp.run(["screencapture", "-l", window_id, output], check=True)
                return {"status": "captured", "path": output, "window_id": int(window_id)}

        # Fallback: capture entire screen
        _sp.run(["screencapture", "-x", output], check=True)
        return {"status": "captured", "path": output}

    elif command == "terminate":
        if not args:
            raise RuntimeError("Usage: simemu do <session> terminate <bundle-id>")
        bundle_id = args[0]
        import re as _re
        if not _re.match(r'^[a-zA-Z0-9._-]+$', bundle_id):
            raise RuntimeError(f"Invalid bundle identifier: {bundle_id}")
        # Graceful quit via osascript, fall back to pkill
        safe_bid = bundle_id.replace("\\", "\\\\").replace('"', '\\"')
        result = _sp.run([
            "osascript", "-e",
            f'tell application id "{safe_bid}" to quit',
        ], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            _sp.run(["pkill", "-f", bundle_id], capture_output=True, check=False)
        return {"status": "terminated", "app": bundle_id}

    elif command == "url":
        if not args:
            raise RuntimeError("Usage: simemu do <session> url <url>")
        url = args[0]
        _sp.run(["open", url], check=True)
        return {"status": "opened", "url": url}

    elif command == "tap":
        if len(args) < 2:
            raise RuntimeError("Usage: simemu do <session> tap <x> <y>")
        x, y = float(args[0]), float(args[1])
        try:
            from Quartz import (  # type: ignore[import-untyped]
                CGEventCreateMouseEvent, CGEventPost,
                kCGEventMouseMoved, kCGEventLeftMouseDown, kCGEventLeftMouseUp,
                kCGHIDEventTap,
            )
            from Quartz import CGPointMake  # type: ignore[import-untyped]

            point = CGPointMake(x, y)
            move = CGEventCreateMouseEvent(None, kCGEventMouseMoved, point, 0)
            CGEventPost(kCGHIDEventTap, move)
            down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, point, 0)
            CGEventPost(kCGHIDEventTap, down)
            up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, point, 0)
            CGEventPost(kCGHIDEventTap, up)
            return {"status": "tapped", "x": x, "y": y}
        except ImportError:
            return {"status": "unsupported", "platform": "macos",
                    "hint": "Tap requires pyobjc-framework-Quartz: pip install pyobjc-framework-Quartz"}

    elif command == "a11y-tap":
        return {"status": "unsupported", "platform": "macos",
                "hint": "Maestro does not support macOS. Use 'tap' with coordinates, "
                        "or AppleScript: 'tell application \"System Events\" to click button \"Name\" ...'."}

    elif command in ("boot", "show", "hide", "renew", "done"):
        # These are handled by the main do_command dispatcher before reaching here.
        # If we get here somehow, they are no-ops for macOS.
        return {"status": "ok", "platform": "macos", "command": command}

    else:
        _MACOS_SUPPORTED = {"install", "launch", "screenshot", "terminate", "url", "tap"}
        return {
            "status": "unsupported",
            "platform": "macos",
            "command": command,
            "hint": f"'{command}' is not available for macOS native sessions. "
                    f"Supported commands: {', '.join(sorted(_MACOS_SUPPORTED))}. "
                    f"macOS apps run natively — most simulator/emulator commands do not apply.",
        }


def do_command(session_id: str, command: str, args: list[str]) -> dict | None:
    """Execute a command on a session. Auto-extends heartbeat."""

    # T-12: Help command
    if command == "help":
        categories = {
            "Session": ["boot", "show", "hide", "renew", "done", "reboot", "present", "stabilize"],
            "App": ["install", "launch", "terminate", "uninstall", "reset-app", "clear-data", "clean-retry",
                     "grant-all", "app-info", "verify-install", "repair-install", "app-container",
                     "is-running", "foreground-app"],
            "UI": ["a11y-tap", "tap", "swipe", "long-press", "scroll", "back", "home",
                   "key", "input", "type-submit", "shake"],
            "Capture": ["screenshot", "proof", "deeplink-proof", "wait-for-render", "video-start",
                        "video-stop", "log-crash"],
            "Navigate": ["url", "maestro"],
            "Alerts": ["dismiss-alert", "accept-alert", "deny-alert", "auto-dismiss",
                       "allow-dialog", "deny-dialog", "permission-grant"],
            "Device": ["appearance", "rotate", "location", "status-bar", "biometrics",
                       "network", "clipboard-set", "clipboard-get"],
            "Files": ["push", "pull", "add-media", "contacts-import"],
            "System": ["keychain-reset", "icloud-sync", "clone", "pair", "font-size",
                       "reduce-motion", "notifications-clear", "a11y-tree", "env"],
            "tvOS Remote": ["focus-move", "focus-select", "remote"],
        }
        result: dict = {"commands": {}}
        for cat, cmds in categories.items():
            result["commands"][cat] = {c: _COMMAND_HELP.get(c, "") for c in cmds}
        return result

    if command == "done":
        session = release(session_id)
        return {"session": session_id, "status": "released"}

    if command == "boot":
        # Explicitly wake a parked session — touch() already does this,
        # but agents expect an explicit boot command
        session = touch(session_id)
        return session.to_agent_json()

    if command == "present":
        session = touch(session_id)
        if session.real_device or session.platform not in ("ios", "watchos", "tvos", "visionos"):
            return {
                "status": "unsupported",
                "platform": session.platform,
                "hint": "present is currently available for iOS-family simulators only.",
            }
        result = ios.present(session.sim_id)
        with _locked_sessions() as (data, save):
            if session_id in data["sessions"]:
                data["sessions"][session_id]["visible"] = True
                save(data)
        return result

    if command == "stabilize":
        session = touch(session_id)
        if session.real_device or session.platform not in ("ios", "watchos", "tvos", "visionos"):
            return {
                "status": "unsupported",
                "platform": session.platform,
                "hint": "stabilize is currently available for iOS-family simulators only.",
            }
        return ios.stabilize(session.sim_id)

    if command in ("visible", "show"):
        session = touch(session_id)
        if not session.real_device and session.platform in ("ios", "watchos", "tvos", "visionos"):
            import subprocess
            window_match = ios._sim_window_match(session.device_name)
            subprocess.run([
                "osascript", "-e",
                f'''tell application "Simulator" to activate
tell application "System Events"
    tell process "Simulator"
        try
            set w to first window whose {window_match}
            set miniaturized of w to false
            perform action "AXRaise" of w
        end try
    end tell
end tell'''
            ], capture_output=True, check=False)
        elif not session.real_device and session.platform == "android":
            if android.current_window_frame(session.sim_id) is None:
                android.shutdown(session.sim_id)
                android.boot(session.sim_id, headless=False)
            current_serial = android.get_android_serial(session.sim_id, retries=8, delay=1.0)
            if current_serial:
                session.pinned_serial = current_serial
        # Persist visibility state
        with _locked_sessions() as (data, save):
            if session_id in data["sessions"]:
                data["sessions"][session_id]["visible"] = True
                if session.pinned_serial:
                    data["sessions"][session_id]["pinned_serial"] = session.pinned_serial
                save(data)
        return {"session": session_id, "status": "visible", "device": session.device_name}

    if command in ("invisible", "hide"):
        session = touch(session_id)
        if not session.real_device:
            window_mgr.apply_window_mode(session.sim_id, session.platform, session.device_name)
        with _locked_sessions() as (data, save):
            if session_id in data["sessions"]:
                data["sessions"][session_id]["visible"] = False
                save(data)
        return {"session": session_id, "status": "invisible", "device": session.device_name}

    if command == "renew":
        hours = None
        if "--hours" in args:
            idx = args.index("--hours")
            if idx + 1 < len(args):
                hours = float(args[idx + 1])
        session = renew(session_id, hours=hours)
        return session.to_agent_json()

    # All other commands: touch heartbeat, then dispatch
    session = touch(session_id)
    sim_id = session.sim_id
    platform = session.platform
    is_real = session.real_device

    # Android session isolation: validate the pinned serial still resolves to this AVD.
    # This prevents cross-session contamination when multiple emulators are running.
    _pinned = session.pinned_serial if (platform == "android" and not is_real) else None
    if _pinned:
        current_serial = android.get_android_serial(sim_id, retries=2, delay=0.5)
        if current_serial and current_serial != _pinned:
            # Serial changed — the emulator restarted on a new port. Update pin.
            _pinned = current_serial
            with _locked_sessions() as (data, save):
                if session_id in data["sessions"]:
                    data["sessions"][session_id]["pinned_serial"] = current_serial
                    save(data)
            session.pinned_serial = current_serial

    # Pass pinned serial as a thread-safe parameter instead of monkey-patching.
    # The monkey-patch approach was not thread-safe: concurrent do_command calls
    # from the HTTP server could overwrite each other's serial binding.
    return _do_command_dispatch(session_id, session, sim_id, platform, is_real, command, args, _pinned)


def _do_command_dispatch(session_id: str, session, sim_id: str, platform: str,
                         is_real: bool, command: str, args: list[str],
                         pinned_serial: str | None = None):
    """Inner dispatch for do_command. pinned_serial is thread-safe — no monkey-patching."""

    android_kwargs = (
        {"pinned_serial": pinned_serial}
        if platform == "android" and not is_real and pinned_serial
        else {}
    )

    def _android_serial_for_session() -> str:
        if is_real:
            return sim_id
        return android._serial(sim_id, pinned=pinned_serial)

    # Update HUD — only for visible sessions (hidden = no overlay needed)
    _is_visible = False
    with _locked_sessions() as (data, save):
        sess_data = data["sessions"].get(session_id, {})
        _is_visible = sess_data.get("visible", False)

    if _is_visible and platform in ("ios", "watchos", "tvos", "visionos"):
        ios._hud_send({
            "mode": "critical",
            "title": "SIMEMU",
            "badge": command.upper(),
            "action": f"{command} on {session.device_name}",
            "detail": f"{session.label}" if session.label else f"Session {session_id}",
            "task": f"simemu do {session_id} {command} {' '.join(args[:2])}".strip(),
            "platform": platform,
            "screen": session.device_name,
            "scenario": command,
        })

    # ── macOS native command dispatch ──────────────────────────────────────
    if platform == "macos":
        return _do_macos_command(session, command, args)

    if command == "install":
        if not args:
            # Auto-install: use the last build artifact
            app_path = _get_build_artifact(session.session_id)
            if not app_path:
                raise RuntimeError(
                    "Usage: simemu do <session> install <path-to-app>\n"
                    "Or run 'simemu do <session> build' first for auto-install."
                )
        else:
            app_path = args[0]
        if is_real and platform == "ios":
            device.ios_install(sim_id, app_path)
        elif platform in ("ios", "watchos", "tvos", "visionos"):
            ios.install(sim_id, app_path)
        else:
            android.install(sim_id, app_path, **android_kwargs)
        return {"status": "installed", "app": app_path}

    elif command == "launch":
        if not args:
            raise RuntimeError("Usage: simemu do <session> launch <bundle-or-package>")
        bundle = args[0]
        extra = args[1:]
        resolved_component = None
        if is_real and platform == "ios":
            device.ios_launch(sim_id, bundle)
        elif platform in ("ios", "watchos", "tvos", "visionos"):
            ios.launch(sim_id, bundle, extra)
        else:
            expected_pkg = bundle.split("/", 1)[0]
            _restore_android_location_if_recorded(session_id, sim_id, android_kwargs)
            # Isolate: force-stop other third-party apps before launch
            stopped = android.stop_other_apps(sim_id, keep=expected_pkg, **android_kwargs)
            if stopped:
                import sys as _sys
                print(json.dumps({
                    "diagnostic": "android_isolation",
                    "stopped_count": len(stopped),
                    "stopped": stopped[:10],
                }), file=_sys.stderr, flush=True)
            resolved_component = android.launch(sim_id, bundle, extra, **android_kwargs)
            # Verify the right package is foregrounded
            actual_fg = android.foreground_app(sim_id, **android_kwargs)
            if actual_fg and actual_fg != expected_pkg:
                import sys as _sys
                print(json.dumps({
                    "diagnostic": "android_launch_foreground_mismatch",
                    "expected": expected_pkg,
                    "actual": actual_fg,
                }), file=_sys.stderr, flush=True)
        launched_pkg = bundle.split("/", 1)[0]
        with _locked_sessions() as (data, save):
            if session_id in data["sessions"]:
                data["sessions"][session_id]["last_app"] = launched_pkg
                save(data)
        provenance_fields = {
            "last_app": launched_pkg,
            "last_launch_args": extra[:3],
        }
        if isinstance(resolved_component, str) and "/" in resolved_component:
            provenance_fields["last_launch_component"] = resolved_component
        elif platform == "android" and "/" in bundle:
            provenance_fields["last_launch_component"] = bundle
        update_provenance(session_id, **provenance_fields)
        return {"status": "launched", "app": bundle}

    elif command == "launch-with-args":
        # Alias for `launch` that makes passing app args explicit.
        # Usage: simemu do <session> launch-with-args <bundle> [arg1 arg2 ...]
        if not args:
            raise RuntimeError("Usage: simemu do <session> launch-with-args <bundle> [arg1 arg2 ...]")
        args = ["launch"] + list(args)
        # Delegate by re-entering via the launch branch above isn't possible here;
        # call ios.launch directly for iOS (Android not supported for arg-passing).
        bundle = args[1]
        extra = args[2:]
        if platform not in ("ios", "watchos", "tvos", "visionos"):
            raise RuntimeError("launch-with-args is iOS-only; Android launch args not supported")
        ios.launch(sim_id, bundle, extra)
        return {"status": "launched", "app": bundle, "args": extra}

    elif command == "tap":
        if platform == "tvos":
            raise RuntimeError(
                "tvOS does not support tap — use focus navigation instead:\n"
                "  simemu do <session> focus-move up/down/left/right\n"
                "  simemu do <session> focus-select\n"
                "  simemu do <session> remote <button>"
            )
        if len(args) < 2:
            raise RuntimeError("Usage: simemu do <session> tap <x> <y>")
        x, y = float(args[0]), float(args[1])
        if platform in ("ios", "watchos", "visionos"):
            ios.tap(sim_id, x, y)
        else:
            android.tap(sim_id, x, y, **android_kwargs)
        return {"status": "tapped", "x": x, "y": y}

    elif command == "swipe":
        if len(args) < 4:
            raise RuntimeError("Usage: simemu do <session> swipe <x1> <y1> <x2> <y2> [--duration ms]")
        x1, y1, x2, y2 = float(args[0]), float(args[1]), float(args[2]), float(args[3])
        duration = 300
        if "--duration" in args:
            idx = args.index("--duration")
            if idx + 1 < len(args):
                duration = int(args[idx + 1])
        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.swipe(sim_id, x1, y1, x2, y2, duration=duration / 1000.0)
        else:
            android.swipe(sim_id, x1, y1, x2, y2, duration=duration, **android_kwargs)
        return {"status": "swiped", "from": [x1, y1], "to": [x2, y2]}

    elif command == "screenshot":
        output = None
        fmt = "png"
        max_size = None
        i = 0
        while i < len(args):
            if args[i] in ("-o", "--output") and i + 1 < len(args):
                output = args[i + 1]
                i += 2
            elif args[i] in ("-f", "--format") and i + 1 < len(args):
                fmt = args[i + 1]
                i += 2
            elif args[i] == "--max-size" and i + 1 < len(args):
                max_size = int(args[i + 1])
                i += 2
            else:
                i += 1

        # T-25: Default max-size from env
        if max_size is None and "SIMEMU_SCREENSHOT_MAX_SIZE" in os.environ:
            max_size = int(os.environ["SIMEMU_SCREENSHOT_MAX_SIZE"])

        if not output:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = Path(os.environ.get("SIMEMU_OUTPUT_DIR", Path.home() / ".simemu"))
            out_dir.mkdir(parents=True, exist_ok=True)
            output = str(out_dir / f"{session_id}_{ts}.{fmt}")

        # T-LU-042: Activate the session's target app before screenshotting so we
        # don't capture SpringBoard when the app was backgrounded.
        _screenshot_target_app = None
        with _locked_sessions() as (data, _save):
            _screenshot_target_app = (
                data["sessions"].get(session_id, {}).get("last_app")
            )

        if (
            _screenshot_target_app
            and not is_real
            and os.environ.get("SIMEMU_DISABLE_SCREENSHOT_ACTIVATE") != "1"
        ):
            if platform in ("ios", "watchos", "visionos"):
                ios.activate_app(sim_id, _screenshot_target_app)
            elif platform == "android":
                _pkg = _android_package_name(_screenshot_target_app)
                try:
                    _restore_android_location_if_recorded(session_id, sim_id, android_kwargs)
                    _current = android.foreground_app(sim_id, retries=2, delay=0.4, **android_kwargs)
                    if _current != _pkg:
                        android.launch(sim_id, _pkg, [], **android_kwargs)
                        _current = android.foreground_app(
                            sim_id, retries=3, delay=0.5, **android_kwargs
                        )
                    if _current != _pkg:
                        raise RuntimeError(
                            f"Screenshot capture aborted — expected Android package '{_pkg}' "
                            f"foreground, got '{_current or 'unknown'}'."
                        )
                except RuntimeError:
                    raise
                except Exception as exc:
                    raise RuntimeError(
                        f"Screenshot capture aborted — could not verify Android package "
                        f"'{_pkg}' before capture: {exc}"
                    ) from exc

        if is_real and platform == "ios":
            device.ios_screenshot(sim_id, output, max_size=max_size)
        elif platform == "tvos":
            ios.tvos_screenshot(sim_id, output, fmt=fmt if fmt != "png" else None, max_size=max_size)
        elif platform in ("ios", "watchos", "visionos"):
            ios.screenshot(sim_id, output, fmt=fmt if fmt != "png" else None, max_size=max_size)
        else:
            android.screenshot(sim_id, output, max_size=max_size, **android_kwargs)
        update_provenance(session_id, last_screenshot=output)
        from .proof import mobile_proof_artifact
        artifact = mobile_proof_artifact(
            kind="screenshot",
            session=session,
            output_path=output,
            status="captured",
            build_path=_get_build_artifact(session_id),
            app=_screenshot_target_app,
        )
        return {"status": "captured", "path": output, "artifact": artifact}

    elif command == "maestro":
        if not args:
            raise RuntimeError("Usage: simemu do <session> maestro <flow.yaml> [extra...]")
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            device_id = sim_id
        else:
            device_id = ""
        flow_files = []
        extra_args = []
        for a in args:
            if a.endswith(".yaml") or a.endswith(".yml"):
                flow_files.append(a)
            else:
                extra_args.append(a)
        if not flow_files:
            flow_files = [args[0]]
            extra_args = args[1:]

        expected_app = _resolve_maestro_target_app(session_id, flow_files)

        if platform in ("ios", "watchos", "tvos", "visionos"):
            _ensure_maestro_target_foreground(
                session_id=session_id,
                platform=platform,
                sim_id=sim_id,
                expected_app=expected_app,
                android_kwargs=android_kwargs,
            )
            cmd, env, debug_output = _prepare_maestro_invocation(
                session_id=session_id,
                platform=platform,
                device_id=device_id,
                flow_files=flow_files,
                extra_args=extra_args,
            )
        else:
            cmd, env, debug_output = [], {}, ""
        with _locked_android_maestro():
            if platform == "android":
                try:
                    device_id, android_kwargs = _preflight_android_maestro_device(
                        session_id=session_id,
                        sim_id=sim_id,
                        android_kwargs=android_kwargs,
                    )
                except RuntimeError as exc:
                    raise RuntimeError(
                        f"Android emulator is not adb-ready for Maestro. {exc} "
                        f"Re-claim with: {session.reclaim_command()}"
                    )
                _ensure_maestro_target_foreground(
                    session_id=session_id,
                    platform=platform,
                    sim_id=sim_id,
                    expected_app=expected_app,
                    android_kwargs=android_kwargs,
                )
                cmd, env, debug_output = _prepare_maestro_invocation(
                    session_id=session_id,
                    platform=platform,
                    device_id=device_id,
                    flow_files=flow_files,
                    extra_args=extra_args,
                )
                _stabilize_android_maestro_driver(device_id)
            keepalive_stop = _start_session_keepalive(session_id)
            try:
                result = _run_maestro_process(cmd, env)
            except _sp.TimeoutExpired:
                from .proof import mobile_proof_artifact
                return {
                    "status": "failed",
                    "exit_code": 124,
                    "debug_output": debug_output,
                    "failure_class": "mobile-flow-timeout",
                    "error": f"Maestro timed out after {MAESTRO_TIMEOUT_SECONDS} seconds and was terminated.",
                    "artifact": mobile_proof_artifact(
                        kind="flow",
                        session=session,
                        status="failed",
                        build_path=_get_build_artifact(session_id),
                        app=expected_app,
                        flow_files=flow_files,
                        debug_output=debug_output,
                        failure_class="mobile-flow-timeout",
                    ),
                }
            finally:
                keepalive_stop.set()
            if result.returncode != 0:
                error = _summarize_maestro_failure(
                    session_id=session_id,
                    platform=platform,
                    debug_output=debug_output,
                )
                if platform == "android" and error:
                    import sys as _sys

                    print(json.dumps({
                        "diagnostic": "android_maestro_bridge_retry",
                        "session": session_id,
                        "sim_id": sim_id,
                    }), file=_sys.stderr, flush=True)
                    try:
                        recovered, retry_debug_output, retry_error = _retry_android_maestro_bridge(
                            session_id=session_id,
                            sim_id=sim_id,
                            expected_app=expected_app,
                            android_kwargs=android_kwargs,
                            flow_files=flow_files,
                            extra_args=extra_args,
                        )
                    except RuntimeError as exc:
                        recovered = False
                        retry_debug_output = debug_output
                        retry_error = f"{error} Auto-retry could not stabilize the emulator: {exc}"
                    if recovered:
                        return {"status": "passed", "recovered": "android_maestro_bridge_retry"}
                    debug_output = retry_debug_output
                    error = retry_error or error

                payload = {
                    "status": "failed",
                    "exit_code": result.returncode,
                    "debug_output": debug_output,
                    "failure_class": "mobile-flow-failed",
                }
                if error:
                    payload["error"] = error
                from .proof import mobile_proof_artifact
                payload["artifact"] = mobile_proof_artifact(
                    kind="flow",
                    session=session,
                    status="failed",
                    build_path=_get_build_artifact(session_id),
                    app=expected_app,
                    flow_files=flow_files,
                    debug_output=debug_output,
                    failure_class="mobile-flow-failed",
                )
                return payload
            from .proof import mobile_proof_artifact
            return {
                "status": "passed",
                "artifact": mobile_proof_artifact(
                    kind="flow",
                    session=session,
                    status="passed",
                    build_path=_get_build_artifact(session_id),
                    app=expected_app,
                    flow_files=flow_files,
                    debug_output=debug_output,
                ),
            }

    elif command == "url":
        if not args:
            raise RuntimeError("Usage: simemu do <session> url <url> [--expect-package <pkg>]")
        url = args[0]
        # Parse --expect-package override
        explicit_expect = None
        if "--expect-package" in args:
            idx = args.index("--expect-package")
            if idx + 1 < len(args):
                explicit_expect = args[idx + 1]

        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.open_url(sim_id, url)
            expected_bundle = explicit_expect
            if not expected_bundle:
                with _locked_sessions() as (data, save):
                    expected_bundle = data["sessions"].get(session_id, {}).get("last_app")
            if expected_bundle:
                handoff_ok = ios.complete_open_url_handoff(sim_id, expected_bundle)
                if not handoff_ok:
                    # Diagnose: what IS in the foreground?
                    actual_fg = ios.foreground_app(sim_id)
                    app_running = ios.is_app_running(sim_id, expected_bundle)
                    if app_running:
                        # T-LU-043: Force-activate the app to dismiss sticky sheet
                        ios.activate_app(sim_id, expected_bundle)
                        import time as _time
                        _time.sleep(0.5)
                        ios.accept_open_app_alert(sim_id, attempts=3, delay=0.3)
                        handoff_ok = ios.is_app_running(sim_id, expected_bundle)
                    if not handoff_ok:
                        diag = {
                            "expected": expected_bundle,
                            "actual_foreground": actual_fg,
                            "app_running": app_running,
                            "url": url,
                        }
                        hint = (
                            f"App '{expected_bundle}' is running but stuck behind a confirmation sheet"
                            if app_running
                            else f"App '{expected_bundle}' never launched — still on SpringBoard or system UI"
                        )
                        raise RuntimeError(
                            f"URL handoff failed: {hint}.\n"
                            f"Diagnostics: {json.dumps(diag)}"
                        )
        else:
            expected_package = explicit_expect
            if not expected_package:
                with _locked_sessions() as (data, save):
                    expected_package = data["sessions"].get(session_id, {}).get("last_app")
            android.open_url(sim_id, url, expected_package=expected_package, **android_kwargs)
            # Post-URL settle — deep link handlers (dialogs, sheets) need time to render
            import time as _time
            _time.sleep(1.0)
            # Verify foreground after URL open — retry to handle dialog transitions
            if expected_package:
                actual_fg = android.foreground_app(sim_id, retries=3, delay=1.0, **android_kwargs)
                if actual_fg is None:
                    # null foreground after URL open — app may have a sheet/dialog
                    # that doesn't register as a resumed activity. Don't error — the
                    # URL may have succeeded, just can't confirm foreground.
                    pass
                elif actual_fg != expected_package:
                    lower = actual_fg.lower()
                    if "launcher" in lower or "home" in lower:
                        # Launcher — app probably crashed on the deep link
                        raise RuntimeError(
                            f"URL opened but app crashed — launcher is foreground instead of '{expected_package}'.\n"
                            f"URL: {url}\n"
                            f"The app may not handle this deep link route."
                        )
                    else:
                        diag = {
                            "expected": expected_package,
                            "actual_foreground": actual_fg,
                            "url": url,
                        }
                        raise RuntimeError(
                            f"URL opened but '{expected_package}' is not foreground on Android. "
                            f"Foreground: '{actual_fg}'. Another app may have intercepted the URL.\n"
                            f"Diagnostics: {json.dumps(diag)}"
                        )
        update_provenance(session_id, last_url=url, last_deep_link=url)
        return {"status": "opened", "url": url}

    elif command == "terminate":
        if not args:
            raise RuntimeError("Usage: simemu do <session> terminate <bundle-or-package>")
        bundle = args[0]
        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.terminate(sim_id, bundle)
        else:
            android.terminate(sim_id, bundle, **android_kwargs)
        return {"status": "terminated", "app": bundle}

    elif command == "uninstall":
        if not args:
            raise RuntimeError("Usage: simemu do <session> uninstall <bundle-or-package>")
        bundle = args[0]
        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.uninstall(sim_id, bundle)
        else:
            android.uninstall(sim_id, bundle, **android_kwargs)
        return {"status": "uninstalled", "app": bundle}

    elif command == "input":
        if not args:
            raise RuntimeError("Usage: simemu do <session> input <text>")
        text = " ".join(args)
        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.input_text(sim_id, text)
        else:
            android.input_text(sim_id, text, **android_kwargs)
        return {"status": "input", "text": text}

    elif command == "long-press":
        if len(args) < 2:
            raise RuntimeError("Usage: simemu do <session> long-press <x> <y> [--duration ms]")
        x, y = float(args[0]), float(args[1])
        duration = 1000
        if "--duration" in args:
            idx = args.index("--duration")
            if idx + 1 < len(args):
                duration = int(args[idx + 1])
        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.long_press(sim_id, x, y, duration=duration / 1000.0)
        else:
            android.long_press(sim_id, x, y, duration=duration, **android_kwargs)
        return {"status": "long-pressed", "x": x, "y": y}

    elif command == "key":
        if not args:
            raise RuntimeError("Usage: simemu do <session> key <key-name>")
        key_name = args[0]
        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.key(sim_id, key_name)
        else:
            android.key(sim_id, key_name, **android_kwargs)
        return {"status": "key_pressed", "key": key_name}

    elif command == "software-keyboard":
        action = args[0] if args else "toggle"
        if platform not in ("ios", "watchos", "tvos", "visionos"):
            raise RuntimeError("software-keyboard is iOS Simulator only")
        ios.software_keyboard(sim_id, action)
        return {"status": "toggled", "target": "software-keyboard"}

    elif command == "focus-move":
        if platform != "tvos":
            raise RuntimeError("focus-move is tvOS only. Use tap/swipe for other platforms.")
        if not args:
            raise RuntimeError("Usage: simemu do <session> focus-move <up|down|left|right>")
        ios.focus_move(sim_id, args[0])
        return {"status": "moved", "direction": args[0]}

    elif command == "focus-select":
        if platform != "tvos":
            raise RuntimeError("focus-select is tvOS only. Use tap for other platforms.")
        ios.focus_select(sim_id)
        return {"status": "selected"}

    elif command == "remote":
        if platform != "tvos":
            raise RuntimeError("remote is tvOS only.")
        if not args:
            raise RuntimeError(
                "Usage: simemu do <session> remote <button>\n"
                "Buttons: up, down, left, right, select, menu, play-pause, home"
            )
        ios.remote_button(sim_id, args[0])
        return {"status": "pressed", "button": args[0]}

    elif command == "appearance":
        if not args:
            raise RuntimeError("Usage: simemu do <session> appearance <light|dark>")
        mode = args[0]
        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.set_appearance(sim_id, mode)
        else:
            android.set_appearance(sim_id, mode, **android_kwargs)
        return {"status": "set", "appearance": mode}

    elif command == "rotate":
        if not args:
            raise RuntimeError("Usage: simemu do <session> rotate <portrait|landscape|left|right>")
        orientation = args[0]
        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.rotate(sim_id, orientation)
        else:
            android.rotate(sim_id, orientation, **android_kwargs)
        return {"status": "rotated", "orientation": orientation}

    elif command == "location":
        if len(args) < 2:
            raise RuntimeError("Usage: simemu do <session> location <lat> <lng>")
        lat, lng = float(args[0]), float(args[1])
        verify_only = "--verify-only" in args
        if platform in ("ios", "watchos", "tvos", "visionos"):
            if verify_only:
                raise RuntimeError("location --verify-only is Android only")
            ios.location(sim_id, lat, lng)
            verification = None
        else:
            if verify_only:
                verified_lat, verified_lng = android.verify_location(sim_id, lat, lng, **android_kwargs)
                verification = {"lat": verified_lat, "lng": verified_lng}
            else:
                verification = android.location(sim_id, lat, lng, **android_kwargs)
                update_provenance(session_id, last_location={"lat": lat, "lng": lng})
        result = {"status": "verified" if verify_only else "set", "lat": lat, "lng": lng}
        if isinstance(verification, dict):
            result["verified"] = verification
        return result

    elif command == "push":
        if len(args) < 2:
            raise RuntimeError("Usage: simemu do <session> push <local> <remote>")
        if platform != "android":
            raise RuntimeError("push is Android only")
        android.push(sim_id, args[0], args[1], **android_kwargs)
        return {"status": "pushed", "remote": args[1]}

    elif command == "pull":
        if len(args) < 2:
            raise RuntimeError("Usage: simemu do <session> pull <remote> <local>")
        if platform != "android":
            raise RuntimeError("pull is Android only")
        android.pull(sim_id, args[0], args[1], **android_kwargs)
        return {"status": "pulled", "local": args[1]}

    elif command == "add-media":
        if not args:
            raise RuntimeError("Usage: simemu do <session> add-media <file>")
        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.add_media(sim_id, args[0])
        else:
            android.add_media(sim_id, args[0], **android_kwargs)
        return {"status": "added", "file": args[0]}

    elif command == "shake":
        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.shake(sim_id)
        else:
            android.shake(sim_id, **android_kwargs)
        return {"status": "shaken"}

    elif command == "status-bar":
        # Parse status-bar flags
        time_str = None
        battery = None
        wifi = None
        network = None
        clear = False
        i = 0
        while i < len(args):
            if args[i] == "--time" and i + 1 < len(args):
                time_str = args[i + 1]
                i += 2
            elif args[i] == "--battery" and i + 1 < len(args):
                battery = int(args[i + 1])
                i += 2
            elif args[i] == "--wifi" and i + 1 < len(args):
                wifi = int(args[i + 1])
                i += 2
            elif args[i] == "--network" and i + 1 < len(args):
                network = args[i + 1]
                i += 2
            elif args[i] == "--clear":
                clear = True
                i += 1
            else:
                i += 1
        if clear:
            if platform in ("ios", "watchos", "tvos", "visionos"):
                ios.status_bar_clear(sim_id)
            else:
                android.status_bar_clear(sim_id, **android_kwargs)
            return {"status": "cleared"}
        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.status_bar(sim_id, time_str=time_str, battery=battery,
                           wifi=wifi, network=network)
        else:
            android.status_bar(sim_id, time_str=time_str, battery=battery,
                               wifi=wifi, network=network, **android_kwargs)
        return {"status": "set"}

    elif command == "dismiss-alert":
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            # T-LU-262: dismiss = deny intent; raise if no path actually worked.
            # Dict-union preserves "method" from the inner call (which path
            # actually clicked the button) and overrides "status" to "dismissed"
            # for caller-API consistency. See TestDoDismissAlert.
            return ios.dismiss_system_alert(sim_id, "deny") | {"status": "dismissed"}
        else:
            _sp.run(["adb", "-s", _android_serial_for_session(),
                      "shell", "input", "keyevent", "KEYCODE_ENTER"],
                     capture_output=True, check=False)
        return {"status": "dismissed"}

    elif command == "accept-alert":
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            # T-LU-262: route through dismiss_system_alert("accept") so the
            # broader _ACCEPT_LABELS (Allow Once / Allow While Using App /
            # Always Allow) are tried, not just the narrow Open/Continue/Allow/OK
            # subset that accept_open_app_alert covers. dismiss_system_alert
            # already raises ios26_alert_unreachable_error on total failure.
            # Codex adversarial P2 (2026-05-14): the previous version failed
            # on real iOS permission dialogs whose affirmative button is one
            # of the "Allow * Using App" variants.
            result = ios.dismiss_system_alert(sim_id, "accept")
            expected_bundle = None
            with _locked_sessions() as (data, save):
                expected_bundle = data["sessions"].get(session_id, {}).get("last_app")
            if expected_bundle:
                ios.complete_open_url_handoff(sim_id, expected_bundle, attempts=3, foreground_timeout=1.0)
            # Mirror the dismiss-alert shape: keep "method" from the inner
            # call, override "status" to the caller-facing verb "accepted".
            return result | {"status": "accepted"}
        else:
            _sp.run(["adb", "-s", _android_serial_for_session(),
                      "shell", "input", "keyevent", "KEYCODE_ENTER"],
                     capture_output=True, check=False)
        return {"status": "accepted"}

    elif command == "deny-alert":
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            # T-LU-262: returns {"status":"denied", ...} or raises RuntimeError.
            return ios.dismiss_system_alert(sim_id, "deny")
        else:
            _sp.run(["adb", "-s", _android_serial_for_session(),
                      "shell", "input", "keyevent", "KEYCODE_BACK"],
                     capture_output=True, check=False)
        return {"status": "denied"}

    elif command == "allow-dialog":
        # Dedicated permission-dialog handler: focuses window first, clicks by label.
        # Usage: simemu do <session> allow-dialog [button-label]
        # Default button: "Allow". Works for notifications, photos, camera, etc.
        if platform not in ("ios", "watchos", "tvos", "visionos"):
            raise RuntimeError("allow-dialog is iOS-only")
        label = args[0] if args else "Allow"
        clicked = ios.permission_dialog_tap(sim_id, label)
        return {"status": "clicked" if clicked else "not_found", "label": label}

    elif command == "deny-dialog":
        # Like allow-dialog but denies. Default button: "Don’t Allow".
        if platform not in ("ios", "watchos", "tvos", "visionos"):
            raise RuntimeError("deny-dialog is iOS-only")
        label = args[0] if args else "Don’t Allow"
        clicked = ios.permission_dialog_tap(sim_id, label)
        return {"status": "clicked" if clicked else "not_found", "label": label}

    elif command == "permission-grant":
        # Pre-authorize a specific permission before app launch.
        # Usage: simemu do <session> permission-grant <bundle> <service>
        # service: notifications, photos, camera, microphone, contacts, location, all
        if len(args) < 2:
            raise RuntimeError("Usage: simemu do <session> permission-grant <bundle> <service>")
        bundle, service = args[0], args[1]
        if platform not in ("ios", "watchos", "tvos", "visionos"):
            raise RuntimeError("permission-grant is iOS-only; use grant-all for Android")
        ios.privacy(sim_id, bundle, "grant", service)
        return {"status": "granted", "app": bundle, "service": service}

    elif command == "grant-all":
        if not args:
            raise RuntimeError("Usage: simemu do <session> grant-all <bundle-or-package>")
        bundle = args[0]
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            services = ["all"]
            for svc in services:
                _sp.run(["xcrun", "simctl", "privacy", sim_id, "grant", svc, bundle],
                         capture_output=True, check=False)
        else:
            serial = _android_serial_for_session()
            permissions = [
                "android.permission.CAMERA",
                "android.permission.RECORD_AUDIO",
                "android.permission.ACCESS_FINE_LOCATION",
                "android.permission.ACCESS_COARSE_LOCATION",
                "android.permission.READ_CONTACTS",
                "android.permission.WRITE_CONTACTS",
                "android.permission.READ_EXTERNAL_STORAGE",
                "android.permission.WRITE_EXTERNAL_STORAGE",
                "android.permission.READ_MEDIA_IMAGES",
                "android.permission.READ_MEDIA_VIDEO",
                "android.permission.POST_NOTIFICATIONS",
            ]
            for perm in permissions:
                _sp.run(["adb", "-s", serial, "shell", "pm", "grant", bundle, perm],
                         capture_output=True, check=False)
        return {"status": "granted", "app": bundle}

    elif command == "clear-data":
        if not args:
            raise RuntimeError("Usage: simemu do <session> clear-data <bundle-or-package>")
        bundle = args[0]
        if platform in ("ios", "watchos", "tvos", "visionos"):
            # iOS: terminate + uninstall + reinstall is the only reliable way
            ios.terminate(sim_id, bundle)
            # Can't easily clear data on iOS sim without uninstall
            return {"status": "terminated", "hint": "iOS: uninstall and reinstall to clear data"}
        else:
            android.clear_data(sim_id, bundle, **android_kwargs)
        return {"status": "cleared", "app": bundle}

    elif command == "clean-retry":
        if not args:
            raise RuntimeError("Usage: simemu do <session> clean-retry <bundle-or-package>")
        bundle = args[0]
        if platform in ("ios", "watchos", "tvos", "visionos"):
            raise RuntimeError(
                "'clean-retry' is Android only. On iOS use "
                "`simemu do <session> reset-app <bundle> <app-path>`."
            )
        android.clear_data(sim_id, bundle, **android_kwargs)
        android.launch(sim_id, bundle, [], **android_kwargs)
        with _locked_sessions() as (data, save):
            if session_id in data["sessions"]:
                data["sessions"][session_id]["last_app"] = bundle.split("/", 1)[0]
                save(data)
        return {
            "status": "clean_retried",
            "app": bundle,
            "hint": "App data cleared and relaunched from a clean state.",
        }

    elif command == "clipboard-set":
        if not args:
            raise RuntimeError("Usage: simemu do <session> clipboard-set <text>")
        text = " ".join(args)
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            _sp.run(["xcrun", "simctl", "pbcopy", sim_id],
                     input=text.encode(), capture_output=True, check=False)
        else:
            android.input_text(sim_id, text, **android_kwargs)
        return {"status": "set", "text": text}

    elif command == "clipboard-get":
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            result = _sp.run(["xcrun", "simctl", "pbpaste", sim_id],
                              capture_output=True, text=True, check=False)
            return {"status": "ok", "text": result.stdout.strip()}
        else:
            return {"status": "unsupported", "hint": "Android clipboard-get not supported"}

    elif command == "build":
        return _do_build(session, sim_id, platform, is_real, args)

    elif command == "env":
        result = {"session": session_id, "platform": platform, "form_factor": session.form_factor}
        if platform in ("ios", "watchos", "tvos", "visionos"):
            result["udid"] = sim_id
        else:
            serial = _android_serial_for_session()
            result["serial"] = serial
        result["device_name"] = session.device_name
        result["os_version"] = session.resolved_os_version or session.os_version
        return result

    elif command == "trace":
        from .trace import export_trace, export_trace_to_file
        output = None
        if "-o" in args:
            idx = args.index("-o")
            if idx + 1 < len(args):
                output = args[idx + 1]
        if output:
            path = export_trace_to_file(session_id, output)
            return {"status": "exported", "path": path}
        return export_trace(session_id)

    # ── high-impact commands ─────────────────────────────────────────────────

    elif command == "auto-dismiss":
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            # Accept any pending alert. Do NOT reset privacy — resetting causes
            # permission dialogs to reappear on next launch (was the old bug).
            ios.permission_dialog_tap(sim_id, "Allow")
            _sp.run(["xcrun", "simctl", "ui", sim_id, "alert", "accept"],
                     capture_output=True, check=False)
            return {"status": "dismissed", "platform": "ios",
                    "hint": "Accepted pending alert"}
        else:
            serial = _android_serial_for_session()
            # Disable window animation scale
            _sp.run(["adb", "-s", serial, "shell", "settings", "put", "global",
                      "window_animation_scale", "0"],
                     capture_output=True, check=False)
            _sp.run(["adb", "-s", serial, "shell", "settings", "put", "global",
                      "transition_animation_scale", "0"],
                     capture_output=True, check=False)
            _sp.run(["adb", "-s", serial, "shell", "settings", "put", "global",
                      "animator_duration_scale", "0"],
                     capture_output=True, check=False)
            # Disable package verifier
            _sp.run(["adb", "-s", serial, "shell", "settings", "put", "global",
                      "package_verifier_enable", "0"],
                     capture_output=True, check=False)
            return {"status": "dismissed", "platform": "android",
                    "hint": "Disabled animations and package verifier"}

    elif command == "wait-for-render":
        import subprocess as _sp
        import time as _time
        seconds = 3
        output = None
        i = 0
        while i < len(args):
            if args[i] in ("-o", "--output") and i + 1 < len(args):
                output = args[i + 1]
                i += 2
            else:
                try:
                    seconds = float(args[i])
                except ValueError:
                    pass
                i += 1
        _time.sleep(seconds)
        # Take screenshot after waiting
        if not output:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = Path(os.environ.get("SIMEMU_OUTPUT_DIR", Path.home() / ".simemu"))
            out_dir.mkdir(parents=True, exist_ok=True)
            output = str(out_dir / f"{session_id}_{ts}.png")
        if is_real and platform == "ios":
            device.ios_screenshot(sim_id, output)
        elif platform in ("ios", "watchos", "tvos", "visionos"):
            ios.screenshot(sim_id, output)
        else:
            android.screenshot(sim_id, output, **android_kwargs)
        return {"status": "captured", "waited": seconds, "path": output}

    elif command == "deeplink-proof":
        import time as _time
        if not args:
            raise RuntimeError("Usage: simemu do <session> deeplink-proof <url> [-o output] [--wait seconds] [--expect-package package]")
        url = args[0]
        output = None
        explicit_expect = None
        wait_seconds = 3.0
        if platform == "android":
            # Android deep links often dispatch before Compose has applied route state.
            wait_seconds = 5.0
        i = 1
        while i < len(args):
            arg = args[i]
            if arg in ("-o", "--output") and i + 1 < len(args):
                output = args[i + 1]
                i += 2
                continue
            if arg in ("--wait", "-w") and i + 1 < len(args):
                try:
                    wait_seconds = float(args[i + 1])
                except ValueError:
                    raise RuntimeError(f"Invalid deeplink-proof wait value: {args[i + 1]}")
                i += 2
                continue
            if arg == "--expect-package" and i + 1 < len(args):
                explicit_expect = args[i + 1]
                i += 2
                continue
            i += 1
        # Open the URL
        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.open_url(sim_id, url)
            expected_bundle = explicit_expect
            with _locked_sessions() as (data, save):
                expected_bundle = expected_bundle or data["sessions"].get(session_id, {}).get("last_app")
            if expected_bundle and not ios.complete_open_url_handoff(sim_id, expected_bundle):
                raise RuntimeError(
                    f"Opened deep link but '{expected_bundle}' never became foreground on iOS."
                )
            if not expected_bundle:
                ios.accept_open_app_alert(sim_id)
        else:
            expected_package = explicit_expect
            with _locked_sessions() as (data, save):
                expected_package = expected_package or data["sessions"].get(session_id, {}).get("last_app")
            android.open_url(sim_id, url, expected_package=expected_package, **android_kwargs)
        # Wait for render
        if wait_seconds > 0:
            _time.sleep(wait_seconds)
        # Screenshot
        if not output:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = Path(os.environ.get("SIMEMU_OUTPUT_DIR", Path.home() / ".simemu"))
            out_dir.mkdir(parents=True, exist_ok=True)
            output = str(out_dir / f"{session_id}_{ts}.png")
        if is_real and platform == "ios":
            device.ios_screenshot(sim_id, output)
        elif platform in ("ios", "watchos", "tvos", "visionos"):
            ios.screenshot(sim_id, output)
        else:
            android.screenshot(sim_id, output, **android_kwargs)
        update_provenance(session_id, last_url=url, last_deep_link=url, last_screenshot=output)
        return {"status": "captured", "url": url, "waited": wait_seconds, "path": output}

    elif command == "proof":
        return _do_proof(session, sim_id, platform, is_real, session_id, args)

    elif command == "reset-app":
        if len(args) < 2:
            raise RuntimeError("Usage: simemu do <session> reset-app <bundle-or-package> <app-path>")
        bundle = args[0]
        app_path = args[1]
        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.terminate(sim_id, bundle)
            ios.uninstall(sim_id, bundle)
            ios.install(sim_id, app_path)
            ios.launch(sim_id, bundle, [])
        else:
            android.terminate(sim_id, bundle, **android_kwargs)
            try:
                android.uninstall(sim_id, bundle, **android_kwargs)
            except Exception as exc:
                _session_log(
                    f"[simemu-session] reset-app continuing after Android uninstall "
                    f"failed for '{bundle}' on '{sim_id}': {exc}"
                )
                try:
                    android.clear_data(sim_id, bundle, **android_kwargs)
                except Exception as clear_exc:
                    _session_log(
                        f"[simemu-session] reset-app Android clear-data fallback "
                        f"also failed for '{bundle}' on '{sim_id}': {clear_exc}"
                    )
            android.install(sim_id, app_path, **android_kwargs)
            android.launch(sim_id, bundle, [], **android_kwargs)
        with _locked_sessions() as (data, save):
            if session_id in data["sessions"]:
                data["sessions"][session_id]["last_app"] = bundle.split("/", 1)[0]
                save(data)
        return {"status": "reset", "app": bundle, "reinstalled_from": app_path}

    elif command == "foreground-app":
        if platform in ("ios", "watchos", "tvos", "visionos"):
            return {"status": "ok", "foreground_app": ios.foreground_app(sim_id)}
        else:
            return {"status": "ok", "foreground_app": android.foreground_app(sim_id, **android_kwargs)}

    elif command == "is-running":
        if not args:
            raise RuntimeError("Usage: simemu do <session> is-running <bundle-or-package>")
        bundle = args[0]
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            result = _sp.run(
                ["xcrun", "simctl", "spawn", sim_id, "launchctl", "list"],
                capture_output=True, text=True, check=False,
            )
            running = bundle in result.stdout
            return {"status": "ok", "app": bundle, "running": running}
        else:
            serial = _android_serial_for_session()
            result = _sp.run(
                ["adb", "-s", serial, "shell", "pidof", bundle],
                capture_output=True, text=True, check=False,
            )
            running = bool(result.stdout.strip())
            pid = result.stdout.strip() if running else None
            return {"status": "ok", "app": bundle, "running": running, "pid": pid}

    # ── medium-impact commands ───────────────────────────────────────────────

    elif command == "network":
        if not args:
            raise RuntimeError("Usage: simemu do <session> network <offline|slow|normal>")
        mode = args[0]
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            return {"status": "unsupported", "platform": "ios",
                    "hint": "iOS Simulator does not support network simulation via simctl. "
                            "Use Network Link Conditioner in System Preferences or "
                            "Apple Configurator profiles."}
        else:
            serial = _android_serial_for_session()
            if mode == "offline":
                _sp.run(["adb", "-s", serial, "shell", "svc", "wifi", "disable"],
                         capture_output=True, check=False)
                _sp.run(["adb", "-s", serial, "shell", "svc", "data", "disable"],
                         capture_output=True, check=False)
            elif mode == "slow":
                # Re-enable networking but throttle via emulator console
                _sp.run(["adb", "-s", serial, "shell", "svc", "wifi", "enable"],
                         capture_output=True, check=False)
                _sp.run(["adb", "-s", serial, "shell", "svc", "data", "enable"],
                         capture_output=True, check=False)
                return {"status": "partial", "network": mode,
                        "hint": "WiFi/data re-enabled. For true throttling use "
                                "emulator -netdelay or -netspeed flags at boot."}
            elif mode == "normal":
                _sp.run(["adb", "-s", serial, "shell", "svc", "wifi", "enable"],
                         capture_output=True, check=False)
                _sp.run(["adb", "-s", serial, "shell", "svc", "data", "enable"],
                         capture_output=True, check=False)
            else:
                raise RuntimeError(f"Unknown network mode '{mode}'. Use: offline, slow, normal")
            return {"status": "set", "network": mode}

    elif command == "keychain-reset":
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            _sp.run(["xcrun", "simctl", "keychain", sim_id, "reset"],
                     capture_output=True, check=False)
            return {"status": "reset", "platform": "ios"}
        else:
            return {"status": "unsupported", "platform": "android",
                    "hint": "Android does not have a keychain equivalent. "
                            "Use clear-data to reset app credentials."}

    elif command == "icloud-sync":
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            _sp.run(["xcrun", "simctl", "icloud_sync", sim_id],
                     capture_output=True, check=False)
            return {"status": "synced", "platform": "ios"}
        else:
            return {"status": "unsupported", "platform": "android",
                    "hint": "iCloud sync is iOS only."}

    elif command == "app-info":
        if not args:
            raise RuntimeError("Usage: simemu do <session> app-info <bundle-or-package>")
        bundle = args[0]
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            result = _sp.run(
                ["xcrun", "simctl", "appinfo", sim_id, bundle],
                capture_output=True, text=True, check=False,
            )
            return {"status": "ok", "app": bundle, "info": result.stdout.strip()}
        else:
            try:
                probe = android.verify_install(sim_id, bundle, timeout=15, **android_kwargs)
            except RuntimeError:
                try:
                    serial = _android_serial_for_session()
                    probe = android._probe_package_state(serial, bundle)
                except RuntimeError:
                    return {
                        "status": "degraded",
                        "app": bundle,
                        "info": "Device temporarily unreachable after activity restart. Retry in a few seconds.",
                    }
            return {"status": "ok", "app": bundle, "info": probe.format_report()}

    elif command == "verify-install":
        if not args:
            raise RuntimeError("Usage: simemu do <session> verify-install <package>")
        if platform in ("ios", "watchos", "tvos", "visionos"):
            raise RuntimeError("'verify-install' is Android only.")
        bundle = args[0]
        probe = android.verify_install(sim_id, bundle, **android_kwargs)
        return {"status": "verified", "app": bundle, "info": probe.format_report()}

    elif command == "repair-install":
        if len(args) < 2:
            raise RuntimeError("Usage: simemu do <session> repair-install <package> <apk-path>")
        if platform in ("ios", "watchos", "tvos", "visionos"):
            raise RuntimeError("'repair-install' is Android only.")
        bundle = args[0]
        app_path = args[1]
        probe = android.repair_install(sim_id, bundle, app_path, **android_kwargs)
        return {"status": "repaired", "app": bundle, "reinstalled_from": app_path, "info": probe.format_report()}

    elif command == "a11y-tree":
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            return {"status": "unsupported", "platform": "ios",
                    "hint": "iOS accessibility hierarchy is not available via simctl. "
                            "Use XCUITest or Maestro for accessibility inspection."}
        else:
            if is_real:
                serial = sim_id
            else:
                try:
                    serial = android._serial(sim_id, pinned=session.pinned_serial)
                except RuntimeError:
                    if os.environ.get("SIMEMU_DISABLE_SESSION_AUTO_REBOOT") == "1" and session.pinned_serial:
                        serial = session.pinned_serial
                    else:
                        raise
            # Dump to a file on device, then cat it back — /dev/tty doesn't capture to stdout.
            # Remove any previous dump first so a transient uiautomator failure cannot
            # return stale XML from a prior foreground surface.
            remote_path = "/sdcard/window_dump.xml"
            _sp.run(["adb", "-s", serial, "shell", "rm", "-f", remote_path],
                    capture_output=True, check=False, timeout=5)
            dump = _sp.run(["adb", "-s", serial, "shell", "uiautomator", "dump", remote_path],
                           capture_output=True, text=True, check=False, timeout=15)
            if dump.returncode != 0:
                hint = (dump.stderr or dump.stdout or "").strip()
                return {"status": "failed",
                        "hint": f"uiautomator dump failed — {hint or 'the app may not expose accessibility nodes.'}"}
            result = _sp.run(["adb", "-s", serial, "shell", "cat", remote_path],
                             capture_output=True, text=True, check=False, timeout=10)
            # Clean up
            _sp.run(["adb", "-s", serial, "shell", "rm", "-f", remote_path],
                    capture_output=True, check=False, timeout=5)
            tree = result.stdout.strip()
            if not tree or "ERROR" in tree:
                return {"status": "failed",
                        "hint": "uiautomator dump failed — the app may not expose accessibility nodes."}
            return {"status": "ok", "tree": tree}

    elif command == "a11y-tap":
        if not args:
            raise RuntimeError("Usage: simemu do <session> a11y-tap <label-text>")
        label_text = " ".join(args)
        import subprocess as _sp
        import tempfile as _tmp

        if platform == "android":
            import xml.etree.ElementTree as _et

            if is_real:
                serial = sim_id
            else:
                serial = android._serial(sim_id, pinned=session.pinned_serial)

            remote_path = "/sdcard/window_dump.xml"
            _sp.run(["adb", "-s", serial, "shell", "rm", "-f", remote_path],
                    capture_output=True, check=False, timeout=5)
            dump = _sp.run(["adb", "-s", serial, "shell", "uiautomator", "dump", remote_path],
                           capture_output=True, text=True, check=False, timeout=15)
            if dump.returncode != 0:
                hint = (dump.stderr or dump.stdout or "").strip()
                return {"status": "failed",
                        "label": label_text,
                        "hint": f"uiautomator dump failed — {hint or 'the app may not expose accessibility nodes.'}"}
            result = _sp.run(["adb", "-s", serial, "shell", "cat", remote_path],
                             capture_output=True, text=True, check=False, timeout=10)
            _sp.run(["adb", "-s", serial, "shell", "rm", "-f", remote_path],
                    capture_output=True, check=False, timeout=5)

            tree = (result.stdout or "").strip()
            if not tree or "ERROR" in tree:
                return {"status": "failed",
                        "label": label_text,
                        "hint": "uiautomator dump failed — the app may not expose accessibility nodes."}

            def _normalized(value: str | None) -> str:
                return " ".join((value or "").split()).casefold()

            target = _normalized(label_text)
            candidates = []
            try:
                root = _et.fromstring(tree)
                for node in root.iter("node"):
                    attrs = node.attrib
                    values = [
                        _normalized(attrs.get("text")),
                        _normalized(attrs.get("content-desc")),
                        _normalized(attrs.get("resource-id")),
                    ]
                    if target in values:
                        candidates.insert(0, attrs)
                    elif any(target and target in value for value in values):
                        candidates.append(attrs)
            except _et.ParseError as exc:
                return {"status": "failed", "label": label_text, "hint": f"uiautomator XML parse failed — {exc}"}

            bounds_re = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
            for attrs in candidates:
                match = bounds_re.fullmatch(attrs.get("bounds", ""))
                if not match:
                    continue
                left, top, right, bottom = (int(part) for part in match.groups())
                if right <= left or bottom <= top:
                    continue
                x = (left + right) // 2
                y = (top + bottom) // 2
                android.tap(sim_id, x, y, **android_kwargs)
                return {"status": "tapped", "label": label_text, "x": x, "y": y, "method": "uiautomator"}

            return {"status": "failed", "label": label_text, "hint": "No matching accessible node with tappable bounds."}

        # Use a single-step Maestro flow for Apple-family platforms.
        safe_label = label_text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        flow_content = f"appId: \"\"\n---\n- tapOn: \"{safe_label}\"\n"
        with _tmp.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(flow_content)
            flow_path = f.name
        try:
            if platform in ("ios", "watchos", "tvos", "visionos"):
                device_id = sim_id
            else:
                if is_real:
                    device_id = sim_id
                else:
                    device_id = android._serial(sim_id, pinned=session.pinned_serial)
            try:
                result = _sp.run(
                    ["maestro", "--device", device_id, "test", flow_path],
                    env=_maestro_env(platform),
                    timeout=MAESTRO_TIMEOUT_SECONDS,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except _sp.TimeoutExpired:
                return {
                    "status": "failed",
                    "label": label_text,
                    "exit_code": 124,
                    "error": f"Maestro a11y-tap timed out after {MAESTRO_TIMEOUT_SECONDS} seconds and was terminated.",
                }
            success = result.returncode == 0
            return {"status": "tapped" if success else "failed",
                    "label": label_text, "exit_code": result.returncode}
        finally:
            Path(flow_path).unlink(missing_ok=True)

    elif command == "type-submit":
        if not args:
            raise RuntimeError("Usage: simemu do <session> type-submit <text...>")
        text = " ".join(args)
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            # Copy text to pasteboard, paste, then press Return
            _sp.run(["xcrun", "simctl", "pbcopy", sim_id],
                     input=text.encode(), capture_output=True, check=False)
            # Paste via Cmd+V keypress
            ios.input_text(sim_id, text)
            _sp.run(["xcrun", "simctl", "io", sim_id, "sendkey", "return"],
                     capture_output=True, check=False)
        else:
            serial = _android_serial_for_session()
            android.input_text(sim_id, text, **android_kwargs)
            _sp.run(["adb", "-s", serial, "shell", "input", "keyevent", "KEYCODE_ENTER"],
                     capture_output=True, check=False)
        return {"status": "typed_and_submitted", "text": text}

    elif command == "scroll":
        if not args:
            raise RuntimeError("Usage: simemu do <session> scroll <up|down|left|right>")
        direction = args[0].lower()
        import subprocess as _sp
        # Define swipe coordinates for each direction (center-based, 1080x1920 logical)
        swipe_map = {
            "up":    (540, 1400, 540, 600),
            "down":  (540, 600, 540, 1400),
            "left":  (900, 960, 180, 960),
            "right": (180, 960, 900, 960),
        }
        if direction not in swipe_map:
            raise RuntimeError(f"Unknown scroll direction '{direction}'. Use: up, down, left, right")
        x1, y1, x2, y2 = swipe_map[direction]
        if platform == "tvos":
            # tvOS: scroll = repeated focus moves in that direction
            for _ in range(3):
                ios.focus_move(sim_id, direction)
                import time as _time
                _time.sleep(0.15)
            return {"status": "scrolled", "direction": direction, "method": "focus_move"}
        elif platform in ("ios", "watchos", "visionos"):
            ios.swipe(sim_id, x1, y1, x2, y2, duration=0.3)
        else:
            android.swipe(sim_id, x1, y1, x2, y2, duration=300, **android_kwargs)
        return {"status": "scrolled", "direction": direction}

    elif command == "back":
        import subprocess as _sp
        if platform == "tvos":
            # tvOS: Menu button = back
            ios.remote_button(sim_id, "menu")
            return {"status": "back", "method": "remote_menu"}
        elif platform in ("ios", "watchos", "visionos"):
            # iOS: swipe from left edge to go back
            ios.swipe(sim_id, 5, 400, 300, 400, duration=0.3)
            return {"status": "back", "method": "edge_swipe"}
        else:
            serial = _android_serial_for_session()
            _sp.run(["adb", "-s", serial, "shell", "input", "keyevent", "KEYCODE_BACK"],
                     capture_output=True, check=False)
            return {"status": "back", "method": "keyevent"}

    elif command == "home":
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            _sp.run(["xcrun", "simctl", "io", sim_id, "sendkey", "home"],
                     capture_output=True, check=False)
        else:
            serial = _android_serial_for_session()
            _sp.run(["adb", "-s", serial, "shell", "input", "keyevent", "KEYCODE_HOME"],
                     capture_output=True, check=False)
        return {"status": "home"}

    elif command == "notifications-clear":
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            return {"status": "unsupported", "platform": "ios",
                    "hint": "iOS Simulator does not expose notification clearing via simctl."}
        else:
            serial = _android_serial_for_session()
            _sp.run(["adb", "-s", serial, "shell", "service", "call",
                      "notification", "1"],
                     capture_output=True, check=False)
            return {"status": "cleared", "platform": "android"}

    elif command == "app-container":
        if not args:
            raise RuntimeError("Usage: simemu do <session> app-container <bundle-or-package>")
        bundle = args[0]
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            result = _sp.run(
                ["xcrun", "simctl", "get_app_container", sim_id, bundle, "data"],
                capture_output=True, text=True, check=False,
            )
            container = result.stdout.strip() if result.returncode == 0 else None
            return {"status": "ok", "app": bundle, "container": container}
        else:
            serial = _android_serial_for_session()
            result = _sp.run(
                ["adb", "-s", serial, "shell", "run-as", bundle, "pwd"],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0:
                return {"status": "ok", "app": bundle, "container": result.stdout.strip()}
            else:
                return {"status": "ok", "app": bundle, "container": None,
                        "hint": "App may not be debuggable (run-as requires debuggable flag)"}

    # ── low-impact commands ──────────────────────────────────────────────────

    elif command == "clone":
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            new_name = args[0] if args else f"{session.device_name}-clone"
            result = _sp.run(
                ["xcrun", "simctl", "clone", sim_id, new_name],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0:
                new_udid = result.stdout.strip()
                return {"status": "cloned", "new_udid": new_udid, "new_name": new_name}
            else:
                return {"status": "failed",
                        "error": result.stderr.strip() or "Clone failed"}
        else:
            return {"status": "unsupported", "platform": "android",
                    "hint": "Android emulator cloning is not supported."}

    elif command == "pair":
        import subprocess as _sp

        def _find_existing_pair_id(watch_udid: str, phone_udid: str) -> str | None:
            result = _sp.run(
                ["xcrun", "simctl", "list", "pairs", "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return None
            try:
                data = json.loads(result.stdout or "{}")
            except json.JSONDecodeError:
                return None

            for pair_id, pair in (data.get("pairs") or {}).items():
                watch = pair.get("watch") or {}
                phone = pair.get("phone") or {}
                if watch.get("udid") == watch_udid and phone.get("udid") == phone_udid:
                    return pair_id
            return None

        def _resolve_target_session(target: str) -> Session | None:
            try:
                return get_session(target)
            except KeyError:
                return None

        if len(args) != 1:
            raise RuntimeError("Usage: simemu do <session> pair <other-session|other-udid>")
        if session.real_device or platform not in ("ios", "watchos"):
            return {
                "status": "unsupported",
                "platform": platform,
                "hint": "pair is only available for iOS/watchOS simulator sessions.",
            }

        other_ref = args[0]
        other_session = _resolve_target_session(other_ref)

        if other_session is not None:
            if other_session.real_device or other_session.platform not in ("ios", "watchos"):
                return {
                    "status": "unsupported",
                    "platform": other_session.platform,
                    "hint": "pair requires an iOS phone session and a watchOS watch session.",
                }
            other_udid = other_session.sim_id
            other_form_factor = other_session.form_factor
        else:
            other_udid = other_ref
            other_form_factor = "phone" if platform == "watchos" else "watch"

        if not sim_id or not other_udid:
            return {"status": "failed", "error": "Both simulator UDIDs are required to pair."}

        if session.form_factor == "watch" and other_form_factor == "phone":
            watch_udid = sim_id
            phone_udid = other_udid
        elif session.form_factor == "phone" and other_form_factor == "watch":
            watch_udid = other_udid
            phone_udid = sim_id
        else:
            return {
                "status": "unsupported",
                "hint": "pair requires exactly one watch session and one phone session.",
            }

        pair_id = _find_existing_pair_id(watch_udid, phone_udid)
        if not pair_id:
            result = _sp.run(
                ["xcrun", "simctl", "pair", watch_udid, phone_udid],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return {
                    "status": "failed",
                    "error": result.stderr.strip() or "Failed to create simulator pair.",
                }
            pair_id = result.stdout.strip()

        activate = _sp.run(
            ["xcrun", "simctl", "pair_activate", pair_id],
            capture_output=True,
            text=True,
            check=False,
        )
        if activate.returncode != 0 and "already active" not in (activate.stderr or "").lower():
            return {
                "status": "failed",
                "error": activate.stderr.strip() or "Failed to activate simulator pair.",
                "pair_id": pair_id,
            }

        return {
            "status": "paired",
            "pair_id": pair_id,
            "watch_udid": watch_udid,
            "phone_udid": phone_udid,
            "hint": "Reboot both simulator sessions to refresh WatchConnectivity pairing state.",
        }

    elif command == "siri":
        if not args:
            raise RuntimeError("Usage: simemu do <session> siri <query...>")
        query = " ".join(args)
        if platform in ("ios", "watchos", "tvos", "visionos"):
            return {"status": "unsupported", "platform": "ios",
                    "hint": f"Siri invocation is not reliably supported via simctl. "
                            f"Query was: '{query}'. Consider using xcrun simctl spawn "
                            f"with notifyutil or Shortcuts automation instead."}
        else:
            return {"status": "unsupported", "platform": "android",
                    "hint": "Siri is iOS only. For Google Assistant use adb broadcast."}

    elif command == "contacts-import":
        if not args:
            raise RuntimeError("Usage: simemu do <session> contacts-import <vcf-file>")
        vcf_path = args[0]
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            _sp.run(["xcrun", "simctl", "addmedia", sim_id, vcf_path],
                     capture_output=True, check=False)
            return {"status": "imported", "file": vcf_path}
        else:
            serial = _android_serial_for_session()
            remote_path = "/sdcard/import_contacts.vcf"
            _sp.run(["adb", "-s", serial, "push", vcf_path, remote_path],
                     capture_output=True, check=False)
            _sp.run(["adb", "-s", serial, "shell", "am", "start",
                      "-a", "android.intent.action.VIEW",
                      "-d", f"file://{remote_path}",
                      "-t", "text/x-vcard"],
                     capture_output=True, check=False)
            return {"status": "imported", "file": vcf_path,
                    "hint": "VCF pushed and import intent launched"}

    elif command == "font-size":
        if not args:
            raise RuntimeError("Usage: simemu do <session> font-size <small|default|large|xlarge>")
        size = args[0].lower()
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            return {"status": "unsupported", "platform": "ios",
                    "hint": "Font size cannot be changed via simctl. "
                            "Use Accessibility settings in the Simulator UI "
                            "or defaults write on the sim plist."}
        else:
            serial = _android_serial_for_session()
            scale_map = {"small": "0.85", "default": "1.0", "large": "1.15", "xlarge": "1.3"}
            scale = scale_map.get(size, "1.0")
            _sp.run(["adb", "-s", serial, "shell", "settings", "put", "system",
                      "font_scale", scale],
                     capture_output=True, check=False)
            return {"status": "set", "font_size": size, "scale": scale}

    elif command == "reduce-motion":
        if not args:
            raise RuntimeError("Usage: simemu do <session> reduce-motion <on|off>")
        mode = args[0].lower()
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            return {"status": "unsupported", "platform": "ios",
                    "hint": "Reduce motion cannot be toggled via simctl. "
                            "Use Accessibility settings in the Simulator UI."}
        else:
            serial = _android_serial_for_session()
            scale = "0" if mode == "on" else "1"
            _sp.run(["adb", "-s", serial, "shell", "settings", "put", "global",
                      "animator_duration_scale", scale],
                     capture_output=True, check=False)
            _sp.run(["adb", "-s", serial, "shell", "settings", "put", "global",
                      "window_animation_scale", scale],
                     capture_output=True, check=False)
            _sp.run(["adb", "-s", serial, "shell", "settings", "put", "global",
                      "transition_animation_scale", scale],
                     capture_output=True, check=False)
            return {"status": "set", "reduce_motion": mode}

    elif command == "log-crash":
        bundle = args[0] if args else None
        if platform in ("ios", "watchos", "tvos", "visionos"):
            log = ios.crash_log(sim_id, bundle_id=bundle)
        else:
            log = android.crash_log(sim_id, package=bundle, **android_kwargs)
        return {"status": "ok", "crash_log": log}

    elif command == "log-tail":
        tag = args[0] if args else None
        level = args[1] if len(args) > 1 else None
        tail_lines = int(args[2]) if len(args) > 2 else 200
        if platform in ("ios", "watchos", "tvos", "visionos"):
            raise RuntimeError("log-tail is only supported on Android right now")
        log = android.log_tail(sim_id, tag=tag, level=level, tail_lines=tail_lines, **android_kwargs)
        return {"status": "ok", "log": log}

    elif command == "video-start":
        output = None
        if "-o" in args:
            idx = args.index("-o")
            if idx + 1 < len(args):
                output = args[idx + 1]
        if not output:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = Path(os.environ.get("SIMEMU_OUTPUT_DIR", Path.home() / ".simemu"))
            out_dir.mkdir(parents=True, exist_ok=True)
            output = str(out_dir / f"{session_id}_{ts}.mp4")
        if platform in ("ios", "watchos", "tvos", "visionos"):
            pid = ios.record_start(sim_id, output)
        else:
            pid = android.record_start(sim_id, output, **android_kwargs)
        return {"status": "recording", "pid": pid, "path": output}

    elif command == "video-stop":
        if not args:
            raise RuntimeError("Usage: simemu do <session> video-stop <pid>")
        pid = int(args[0])
        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.record_stop(pid)
        else:
            android.record_stop(pid)
        return {"status": "stopped", "pid": pid}

    elif command == "reboot":
        import subprocess as _sp
        if platform in ("ios", "watchos", "tvos", "visionos"):
            ios.shutdown(sim_id)
            ios.boot(sim_id)
            return {"status": "rebooted", "platform": "ios"}
        else:
            serial = _android_serial_for_session()
            _sp.run(["adb", "-s", serial, "reboot"],
                     capture_output=True, check=False)
            return {"status": "rebooted", "platform": "android"}

    else:
        raise RuntimeError(
            f"Unknown command '{command}'. Available: boot, show, hide, install, launch, tap, swipe, "
            f"screenshot, maestro, url, done, renew, terminate, uninstall, input, "
            f"long-press, key, appearance, rotate, location, push, pull, add-media, "
            f"dismiss-alert, accept-alert, deny-alert, deny-dialog, allow-dialog, "
            f"permission-grant, grant-all, clear-data, clean-retry, "
            f"clipboard-set, clipboard-get, shake, status-bar, build, env, "
            f"auto-dismiss, wait-for-render, deeplink-proof, reset-app, launch-with-args, "
            f"foreground-app, is-running, network, keychain-reset, icloud-sync, "
            f"app-info, a11y-tree, a11y-tap, type-submit, scroll, back, home, "
            f"software-keyboard, notifications-clear, app-container, clone, siri, contacts-import, "
            f"pair, font-size, reduce-motion, log-crash, video-start, video-stop, reboot"
        )


def _do_proof(session, sim_id: str, platform: str, is_real: bool, session_id: str, args: list[str]) -> dict:
    """Full proof capture: normalize → verify → screenshot → metadata.

    Usage: simemu do <session> proof [-o output.png] [--url <deep-link>]
           [--appearance light|dark] [--wait <seconds>] [--label <name>]
    """
    import subprocess as _sp
    import time as _time

    android_kwargs = (
        {"pinned_serial": session.pinned_serial}
        if platform == "android" and not is_real and session.pinned_serial
        else {}
    )

    # Parse flags
    output = None
    url = None
    appearance = None
    wait_seconds = 2.0
    label = ""
    max_size = None
    i = 0
    while i < len(args):
        if args[i] == "-o" and i + 1 < len(args):
            output = args[i + 1]; i += 2
        elif args[i] == "--url" and i + 1 < len(args):
            url = args[i + 1]; i += 2
        elif args[i] == "--appearance" and i + 1 < len(args):
            appearance = args[i + 1]; i += 2
        elif args[i] == "--wait" and i + 1 < len(args):
            wait_seconds = float(args[i + 1]); i += 2
        elif args[i] == "--label" and i + 1 < len(args):
            label = args[i + 1]; i += 2
        elif args[i] == "--max-size" and i + 1 < len(args):
            max_size = int(args[i + 1]); i += 2
        else:
            i += 1

    steps: list[str] = []
    errors: list[str] = []

    # ── Step 1: Dismiss system dialogs ──────────────────────────────────
    if platform in ("ios", "watchos", "tvos", "visionos"):
        try:
            ios.accept_open_app_alert(sim_id, attempts=2, delay=0.2)
            steps.append("dismiss_alerts")
        except Exception:
            pass
    else:
        try:
            android.dismiss_system_dialogs(sim_id, **android_kwargs)
            steps.append("dismiss_dialogs")
        except Exception:
            pass

    # ── Step 2: Set appearance ──────────────────────────────────────────
    if appearance:
        try:
            if platform in ("ios", "watchos", "tvos", "visionos"):
                ios.set_appearance(sim_id, appearance)
            else:
                android.set_appearance(sim_id, appearance, **android_kwargs)
            steps.append(f"appearance:{appearance}")
        except Exception as e:
            errors.append(f"appearance: {e}")

    # ── Step 3: Clean status bar (iOS) ──────────────────────────────────
    if platform in ("ios", "watchos", "tvos", "visionos") and not is_real:
        try:
            ios.status_bar(sim_id, time_str="9:41", battery=100, wifi=3, network="wifi")
            steps.append("status_bar:9:41")
        except Exception as e:
            errors.append(f"status_bar: {e}")

    # ── Step 4: Isolate (Android) ───────────────────────────────────────
    if platform == "android":
        expected_pkg = None
        with _locked_sessions() as (data, save):
            expected_pkg = data["sessions"].get(session_id, {}).get("last_app")
        if expected_pkg:
            try:
                android.stop_other_apps(sim_id, keep=expected_pkg, **android_kwargs)
                steps.append(f"isolate:{expected_pkg}")
            except Exception:
                pass

    # ── Step 5: Open URL if provided ────────────────────────────────────
    if url:
        try:
            if platform in ("ios", "watchos", "tvos", "visionos"):
                ios.open_url(sim_id, url)
                expected_bundle = None
                with _locked_sessions() as (data, save):
                    expected_bundle = data["sessions"].get(session_id, {}).get("last_app")
                if expected_bundle:
                    if not ios.complete_open_url_handoff(sim_id, expected_bundle):
                        actual = ios.foreground_app(sim_id)
                        errors.append(f"url_handoff: expected {expected_bundle}, got {actual}")
            else:
                expected_pkg_url = None
                with _locked_sessions() as (data, save):
                    expected_pkg_url = data["sessions"].get(session_id, {}).get("last_app")
                android.open_url(sim_id, url, expected_package=expected_pkg_url, **android_kwargs)
            steps.append(f"url:{url[:60]}")
        except Exception as e:
            errors.append(f"url: {e}")

    # ── Step 6: Wait for render ─────────────────────────────────────────
    _time.sleep(wait_seconds)
    steps.append(f"wait:{wait_seconds}s")

    # ── Step 7: Verify foreground ───────────────────────────────────────
    expected_app = None
    with _locked_sessions() as (data, save):
        expected_app = data["sessions"].get(session_id, {}).get("last_app")

    actual_fg = None
    if expected_app:
        if platform in ("ios", "watchos", "tvos", "visionos"):
            actual_fg = ios.foreground_app(sim_id)
        else:
            actual_fg = android.foreground_app(sim_id, **android_kwargs)
        if actual_fg and actual_fg != expected_app:
            # T-LU-042: Try to activate the expected app before flagging as error
            if platform in ("ios", "watchos", "visionos"):
                if ios.activate_app(sim_id, expected_app):
                    steps.append(f"reactivated:{expected_app}")
                    actual_fg = expected_app
            elif platform == "android":
                _epkg = _android_package_name(expected_app)
                try:
                    android.launch(sim_id, _epkg, [], **android_kwargs)
                    steps.append(f"reactivated:{_epkg}")
                    actual_fg = android.foreground_app(
                        sim_id, retries=3, delay=0.5, **android_kwargs
                    )
                except Exception:
                    pass
        elif platform == "android" and actual_fg is None:
            _epkg = _android_package_name(expected_app)
            try:
                android.launch(sim_id, _epkg, [], **android_kwargs)
                steps.append(f"reactivated:{_epkg}")
                actual_fg = android.foreground_app(
                    sim_id, retries=3, delay=0.5, **android_kwargs
                )
            except Exception:
                pass

        if platform == "android" and expected_app:
            expected_compare = _android_package_name(expected_app)
        else:
            expected_compare = expected_app
        if expected_compare and actual_fg != expected_compare:
            errors.append(
                f"foreground_mismatch: expected={expected_compare} actual={actual_fg or 'unknown'}"
            )

    # ── Step 8: Fail if critical errors ─────────────────────────────────
    critical = [e for e in errors if "foreground_mismatch" in e or "url_handoff" in e]
    if critical:
        raise RuntimeError(
            f"Proof capture aborted — device state is not trustworthy.\n"
            f"Errors: {'; '.join(critical)}\n"
            f"Steps completed: {', '.join(steps)}"
        )

    # ── Step 9: Capture screenshot ──────────────────────────────────────
    if not output:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(os.environ.get("SIMEMU_OUTPUT_DIR",
                       os.environ.get("PROJECT_SCREENSHOT_DIR", Path.home() / ".simemu")))
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = f"_{label}" if label else ""
        output = str(out_dir / f"{session_id}_proof{suffix}_{ts}.png")

    if is_real and platform == "ios":
        device.ios_screenshot(sim_id, output, max_size=max_size)
    elif platform in ("ios", "watchos", "tvos", "visionos"):
        ios.screenshot(sim_id, output, max_size=max_size)
    else:
        android.screenshot(sim_id, output, max_size=max_size, settle_ms=800, **android_kwargs)
    steps.append(f"screenshot:{output}")

    # ── Step 10: Store provenance ───────────────────────────────────────
    proof_metadata = {
        "steps": steps,
        "url": url,
        "appearance": appearance,
        "wait_seconds": wait_seconds,
        "label": label,
        "foreground_app": actual_fg,
        "expected_app": expected_app,
        "warnings": [e for e in errors if "foreground_mismatch" not in e],
    }
    from .proof import mobile_proof_artifact
    artifact = mobile_proof_artifact(
        kind="screenshot-proof",
        session=session,
        output_path=output,
        status="proved",
        build_path=_get_build_artifact(session_id),
        app=expected_app,
        metadata=dict(proof_metadata),
    )
    proof_metadata["artifact"] = artifact
    update_provenance(
        session_id,
        last_screenshot=output,
        last_url=url,
        last_proof=proof_metadata,
    )

    return {
        "status": "proved",
        "path": output,
        "steps": steps,
        "warnings": errors,
        "metadata": proof_metadata,
        "artifact": artifact,
    }


def _do_build(session, sim_id: str, platform: str, is_real: bool, args: list[str]) -> dict:
    """Build an app and store the artifact path in session state for auto-install."""
    import json as _json
    import subprocess
    from pathlib import Path

    # Parse flags
    variant = None
    clean = False
    test = False
    raw_cmd = None
    verbose = False
    i = 0
    while i < len(args):
        if args[i] == "--variant" and i + 1 < len(args):
            variant = args[i + 1]; i += 2
        elif args[i] == "--clean":
            clean = True; i += 1
        elif args[i] == "--test":
            test = True; i += 1
        elif args[i] == "--raw" and i + 1 < len(args):
            raw_cmd = args[i + 1]; i += 2
        elif args[i] == "--verbose":
            verbose = True; i += 1
        else:
            i += 1

    # Raw mode — escape hatch
    if raw_cmd:
        import shlex
        try:
            cmd_parts = shlex.split(raw_cmd)
        except ValueError as e:
            raise RuntimeError(f"Invalid build command: {e}")
        result = subprocess.run(cmd_parts, capture_output=not verbose, text=True)
        if result.returncode != 0:
            err = result.stderr[:2000] if result.stderr else ""
            raise RuntimeError(f"Build failed (exit {result.returncode}):\n{raw_cmd}\n{err}")
        return {"status": "built", "mode": "raw", "command": raw_cmd}

    # Find execution.yaml in the project
    cwd = Path.cwd()
    exec_yaml = cwd / "keel" / "execution.yaml"
    build_config = None

    if exec_yaml.exists():
        content = exec_yaml.read_text()
        build_config = _parse_build_variants(content)

    if not build_config:
        raise RuntimeError(
            "No buildVariants in keel/execution.yaml. Either:\n"
            "  1. Add buildVariants config to keel/execution.yaml\n"
            "  2. Use --raw \"xcodebuild -scheme MyApp build\" as escape hatch"
        )

    # Resolve variant — default to first one
    variant_names = list(build_config.keys())
    if not variant:
        variant = variant_names[0]

    if variant not in build_config:
        raise RuntimeError(
            f"Unknown variant '{variant}'. Available: {', '.join(variant_names)}"
        )

    variant_cfg = build_config[variant]

    if platform in ("ios", "watchos", "tvos", "visionos"):
        ios_cfg = variant_cfg.get("ios", {})
        scheme = ios_cfg.get("scheme", variant)
        project = ios_cfg.get("project")
        workspace = ios_cfg.get("workspace")
        configuration = ios_cfg.get("configuration", "Debug")

        cmd_parts = ["xcodebuild"]
        if workspace:
            cmd_parts += ["-workspace", workspace]
        elif project:
            cmd_parts += ["-project", project]
        cmd_parts += ["-scheme", scheme]
        cmd_parts += ["-destination", f"id={sim_id}"]
        cmd_parts += ["-configuration", configuration]
        cmd_parts += ["CODE_SIGNING_ALLOWED=NO"]
        if clean:
            cmd_parts.append("clean")
        cmd_parts.append("test" if test else "build")

        print(f"[simemu] building iOS: {scheme} ({configuration}) → {sim_id[:8]}...")
        result = subprocess.run(cmd_parts, capture_output=not verbose, text=True)
        if result.returncode != 0:
            err = result.stderr[:3000] if result.stderr else result.stdout[-3000:] if result.stdout else ""
            raise RuntimeError(
                f"iOS build failed (exit {result.returncode}):\n"
                f"  {' '.join(cmd_parts)}\n{err}"
            )

        # Find the built .app
        app_path = _find_ios_artifact(scheme, configuration)
        if app_path:
            _store_build_artifact(session.session_id, str(app_path))

        return {
            "status": "built",
            "platform": "ios",
            "variant": variant,
            "scheme": scheme,
            "configuration": configuration,
            "app": str(app_path) if app_path else None,
        }

    else:
        android_cfg = variant_cfg.get("android", {})
        task = android_cfg.get("task", f"assemble{variant.capitalize()}")

        cmd_parts = ["./gradlew"]
        if clean:
            cmd_parts.append("clean")
        cmd_parts.append(task)

        print(f"[simemu] building Android: {task}...")
        result = subprocess.run(cmd_parts, capture_output=not verbose, text=True)
        if result.returncode != 0:
            err = result.stderr[:3000] if result.stderr else result.stdout[-3000:] if result.stdout else ""
            raise RuntimeError(
                f"Android build failed (exit {result.returncode}):\n"
                f"  {' '.join(cmd_parts)}\n{err}"
            )

        # Find the built .apk
        apk_path = _find_android_artifact(task)
        if apk_path:
            _store_build_artifact(session.session_id, str(apk_path))

        return {
            "status": "built",
            "platform": "android",
            "variant": variant,
            "task": task,
            "apk": str(apk_path) if apk_path else None,
        }


def _parse_build_variants(yaml_content: str) -> dict | None:
    """Parse buildVariants from execution.yaml. Minimal YAML parser."""
    lines = yaml_content.split("\n")
    in_variants = False
    current_variant = None
    current_platform = None
    variants: dict = {}

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Top-level key
        if not line.startswith(" ") and not line.startswith("\t"):
            if stripped == "buildVariants:":
                in_variants = True
            else:
                in_variants = False
            continue

        if not in_variants:
            continue

        indent = len(line) - len(line.lstrip())

        # Variant name (indent 2)
        if indent == 2 and stripped.endswith(":"):
            current_variant = stripped[:-1].strip()
            variants[current_variant] = {}
            current_platform = None
            continue

        # Platform (indent 4)
        if indent == 4 and stripped.endswith(":") and current_variant:
            current_platform = stripped[:-1].strip()
            variants[current_variant][current_platform] = {}
            continue

        # Key: value (indent 6)
        if indent == 6 and ": " in stripped and current_variant and current_platform:
            key, _, value = stripped.partition(": ")
            variants[current_variant][current_platform][key.strip()] = value.strip()

    return variants if variants else None


def _find_ios_artifact(scheme: str, configuration: str):
    """Find the most recently built .app in DerivedData."""
    from pathlib import Path
    import os

    derived = Path.home() / "Library" / "Developer" / "Xcode" / "DerivedData"
    if not derived.exists():
        return None

    candidates = []
    config_dir = f"{configuration}-iphonesimulator"
    for dd in derived.iterdir():
        products = dd / "Build" / "Products" / config_dir
        if products.exists():
            for app in products.glob("*.app"):
                candidates.append((app, os.path.getmtime(app)))

    if not candidates:
        return None

    # Return most recently modified
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]


def _find_android_artifact(task: str):
    """Find the built APK from gradle output."""
    from pathlib import Path

    # Common output locations
    for pattern in [
        "app/build/outputs/apk/**/*.apk",
        "build/outputs/apk/**/*.apk",
    ]:
        apks = list(Path.cwd().glob(pattern))
        if apks:
            # Prefer debug APK matching the task
            debug = [a for a in apks if "debug" in a.name.lower()]
            return debug[0] if debug else apks[0]
    return None


def _store_build_artifact(session_id: str, artifact_path: str):
    """Store the build artifact path in session state for auto-install."""
    with _locked_sessions() as (data, save):
        if session_id in data.get("sessions", {}):
            data["sessions"][session_id]["last_build_artifact"] = artifact_path
            save(data)


def _get_build_artifact(session_id: str) -> str | None:
    """Get the stored build artifact path."""
    data = _read_sessions_raw()
    session_data = data.get("sessions", {}).get(session_id, {})
    return session_data.get("last_build_artifact")


# ── lifecycle management ─────────────────────────────────────────────────────

def lifecycle_tick() -> list[str]:
    """Called periodically (every 60s). Transitions sessions through lifecycle states.

    Returns list of session IDs that changed state.
    """
    now = datetime.now(timezone.utc)
    changed = []

    with _locked_sessions() as (data, save):
        dirty = False
        for sid, raw in list(data["sessions"].items()):
            session = _session_from_dict(raw)
            if session.status in ("expired", "released"):
                continue

            hb = datetime.fromisoformat(session.heartbeat_at)
            idle_seconds = (now - hb).total_seconds()

            new_status = session.status

            if idle_seconds >= EXPIRE_TIMEOUT:
                new_status = "expired"
            elif idle_seconds >= IDLE_TIMEOUT + PARK_TIMEOUT and session.status in ("active", "idle"):
                new_status = "parked"
            elif idle_seconds >= IDLE_TIMEOUT and session.status == "active":
                new_status = "idle"

            if new_status != session.status:
                old_status = session.status
                data["sessions"][sid]["status"] = new_status
                data["sessions"][sid]["expires_at"] = _compute_expires_at(new_status, session.heartbeat_at)
                dirty = True
                changed.append(sid)

                # Shutdown device when parking
                if new_status == "parked" and not session.real_device:
                    try:
                        if session.platform in ("ios", "watchos", "tvos", "visionos"):
                            ios.shutdown(session.sim_id)
                        else:
                            android.shutdown(session.sim_id)
                    except Exception:
                        pass  # device may already be off

                _session_log(
                    f"[simemu-session] '{sid}' {old_status} → {new_status} "
                    f"(idle {idle_seconds / 60:.0f}m)"
                )

        reconciled = _reconcile_android_sessions_locked(data)
        if reconciled:
            dirty = True
            changed.extend(reconciled)

        if dirty:
            save(data)

    return changed


# ── memory budget ────────────────────────────────────────────────────────────

def _estimated_memory_mb() -> int:
    """Estimate total memory usage of all active/idle sessions."""
    total = 0
    for session in get_active_sessions().values():
        if session.status != "parked":  # parked devices are shutdown
            total += _DEVICE_MEMORY_MB.get(session.platform, 2048)
    return total


def _memory_budget_mb() -> int:
    """Return configured memory budget in MB."""
    env = os.environ.get("SIMEMU_MEMORY_BUDGET_MB")
    if env:
        return int(env)
    return DEFAULT_MEMORY_BUDGET_MB


def _enforce_memory_budget_if_needed(platform: str) -> None:
    """Park idle sessions if claiming a new device would exceed the memory budget."""
    needed = _DEVICE_MEMORY_MB.get(platform, 2048)
    budget = _memory_budget_mb()
    current = _estimated_memory_mb()

    if current + needed <= budget:
        return

    # Try parking idle sessions to make room, oldest first
    sessions_by_idle = sorted(
        [
            (sid, s) for sid, s in get_active_sessions().items()
            if s.status == "idle"
        ],
        key=lambda x: x[1].heartbeat_at,
    )

    for sid, session in sessions_by_idle:
        if current + needed <= budget:
            break
        _park_session(sid, session)
        current -= _DEVICE_MEMORY_MB.get(session.platform, 2048)

    # If still over budget, check if we can park active sessions (oldest first)
    if current + needed > budget:
        active_sessions = sorted(
            [
                (sid, s) for sid, s in get_active_sessions().items()
                if s.status == "active"
            ],
            key=lambda x: x[1].heartbeat_at,
        )
        # Don't park the most recently active sessions
        for sid, session in active_sessions[:-1]:  # keep at least one
            if current + needed <= budget:
                break
            _park_session(sid, session)
            current -= _DEVICE_MEMORY_MB.get(session.platform, 2048)

    if current + needed > budget:
        raise SessionError(
            error="memory_budget_exceeded",
            session="",
            hint=f"All devices busy (using {current}MB of {budget}MB budget). "
                 f"Release idle sessions first or increase SIMEMU_MEMORY_BUDGET_MB.",
        )


def _park_session(session_id: str, session: Session) -> None:
    """Park a session: shut down its device but keep the session."""
    with _locked_sessions() as (data, save):
        if session_id in data["sessions"]:
            data["sessions"][session_id]["status"] = "parked"
            data["sessions"][session_id]["expires_at"] = _compute_expires_at(
                "parked", session.heartbeat_at
            )
            save(data)

    if not session.real_device:
        try:
            if session.platform in ("ios", "watchos", "tvos", "visionos"):
                ios.shutdown(session.sim_id)
            else:
                android.shutdown(session.sim_id)
        except Exception:
            pass

    _session_log(f"[simemu-session] Parked '{session_id}' to free memory")


# ── errors ───────────────────────────────────────────────────────────────────

class SessionError(RuntimeError):
    """Error with structured JSON output and actionable hints."""

    def __init__(self, error: str, session: str, hint: str, **extra):
        self.error_type = error
        self.session = session
        self.hint = hint
        self.extra = extra
        super().__init__(hint)

    def to_json(self) -> dict:
        result = {
            "error": self.error_type,
            "session": self.session,
            "hint": self.hint,
        }
        result.update(self.extra)
        return result


# ── T-15: Command history ────────────────────────────────────────────────────

def _log_command(session_id: str, command: str, args: list[str]) -> None:
    """Append a command to the session's history log."""
    log_dir = state.state_dir() / "history"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{session_id}.log"
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"{ts} {command} {' '.join(args[:5])}\n"
    try:
        with log_file.open("a") as f:
            f.write(line)
    except OSError:
        pass


def get_command_history(session_id: str) -> list[str]:
    """Return the command history for a session."""
    log_file = state.state_dir() / "history" / f"{session_id}.log"
    if not log_file.exists():
        return []
    return log_file.read_text().strip().splitlines()


# ── T-LU-019: Session provenance ────────────────────────────────────────────

def update_provenance(session_id: str, **fields) -> None:
    """Update proof provenance metadata for a session.

    Stored fields: last_app, last_url, last_screenshot, last_build,
    last_deep_link, render_wait_ms, proof_metadata (arbitrary dict).

    Provenance survives normal session writes and recovery.
    """
    with _locked_sessions() as (data, save):
        session_data = data["sessions"].get(session_id)
        if not session_data:
            return
        provenance = session_data.setdefault("provenance", {})
        provenance["updated_at"] = _now_iso()
        for key, value in fields.items():
            provenance[key] = value
        save(data)


def get_provenance(session_id: str) -> dict:
    """Return the current proof provenance for a session."""
    data = _read_sessions_raw()
    session_data = data["sessions"].get(session_id, {})
    return session_data.get("provenance", {})


# ── T-29: Safe android serial helper ─────────────────────────────────────────

def _android_serial(sim_id: str, pinned_serial: str | None = None) -> str:
    """Get Android serial, raising a clear error if not available."""
    serial = android._serial(sim_id, pinned=pinned_serial) if pinned_serial else android.get_serial(sim_id)
    if not serial:
        raise RuntimeError(
            f"Android emulator '{sim_id}' is not running or not adb-ready. "
            f"Try: simemu do <session> reboot"
        )
    return serial


# ── T-31: Rate limiting ──────────────────────────────────────────────────────

MAX_ACTIVE_PER_AGENT = 8


def _check_rate_limit(agent: str) -> None:
    """Raise if agent has too many active sessions."""
    active = get_active_sessions()
    agent_count = sum(1 for s in active.values()
                      if s.agent == agent and s.status in ("active", "idle"))
    if agent_count >= MAX_ACTIVE_PER_AGENT:
        raise SessionError(
            error="rate_limited",
            session="",
            hint=f"Agent '{agent}' has {agent_count} active sessions (max {MAX_ACTIVE_PER_AGENT}). "
                 f"Release some first: simemu do <session> done",
        )
