"""
iOS simulator operations via xcrun simctl.
All functions take a UDID and operate on that specific simulator.
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Optional


def _simctl(*args, capture: bool = False, check: bool = True) -> Optional[str]:
    cmd = ["xcrun", "simctl"] + list(args)
    if capture:
        result = subprocess.run(cmd, capture_output=True, text=True, check=check)
        return result.stdout.strip()
    else:
        subprocess.run(cmd, check=check)
        return None


def _is_booted(udid: str) -> bool:
    out = subprocess.check_output(
        ["xcrun", "simctl", "list", "devices", "--json"],
        stderr=subprocess.DEVNULL,
    )
    data = json.loads(out)
    for devices in data["devices"].values():
        for dev in devices:
            if dev["udid"] == udid:
                return dev["state"] == "Booted"
    return False


def boot(udid: str, minimize: bool = False) -> None:
    """Boot simulator if not already booted.

    minimize is accepted for API compatibility but ignored — moving the iOS
    Simulator window does not save memory, so windows stay where they are.
    All simemu operations (screenshots, gestures) work without window focus.
    """
    if _is_booted(udid):
        return
    _simctl("boot", udid)
    _simctl("bootstatus", udid, "-b")


def _ensure_booted(udid: str) -> None:
    """Auto-boot if needed. Called transparently by all proxy commands."""
    if not _is_booted(udid):
        print(f"Simulator not running, booting...", flush=True)
        boot(udid)


def shutdown(udid: str) -> None:
    _simctl("shutdown", udid, check=False)


def install(udid: str, app_path: str, timeout: int = 120) -> None:
    _ensure_booted(udid)
    """
    Install an app onto the simulator.
    Accepts .app directories or .ipa files (auto-extracts IPA → .app).
    timeout: seconds before giving up (default 120).
    """
    path = Path(app_path)
    if not path.exists():
        raise RuntimeError(f"App path not found: {app_path}")

    def _install_path(p: str) -> None:
        cmd = ["xcrun", "simctl", "install", udid, p]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Install timed out after {timeout}s. The simulator may be unresponsive. "
                f"Try: simemu reboot <slug>"
            )
        if result.returncode != 0:
            raise RuntimeError(f"Install failed: {result.stderr.strip() or result.stdout.strip()}")

    if path.suffix == ".ipa":
        extracted = _extract_ipa(path)
        try:
            _install_path(extracted)
        finally:
            shutil.rmtree(Path(extracted).parent.parent, ignore_errors=True)
    elif path.suffix == ".app" or path.is_dir():
        _install_path(str(path))
    else:
        raise RuntimeError(f"Unsupported app format: {path.suffix}. Use .app or .ipa")


def _extract_ipa(ipa_path: Path) -> str:
    """Extract .ipa to a temp directory, return path to the .app bundle."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="simemu_ipa_"))
    with zipfile.ZipFile(ipa_path) as z:
        z.extractall(tmp_dir)

    payload = tmp_dir / "Payload"
    if not payload.exists():
        raise RuntimeError(f"Invalid .ipa: no Payload directory in {ipa_path}")

    apps = list(payload.glob("*.app"))
    if not apps:
        raise RuntimeError(f"No .app bundle found in {ipa_path}/Payload/")

    return str(apps[0])


def launch(udid: str, bundle_id: str, args: list[str] | None = None) -> None:
    _ensure_booted(udid)
    cmd_args = ["launch", udid, bundle_id] + (args or [])
    _simctl(*cmd_args)


def terminate(udid: str, bundle_id: str) -> None:
    _ensure_booted(udid)
    _simctl("terminate", udid, bundle_id, check=False)


def uninstall(udid: str, bundle_id: str) -> None:
    _ensure_booted(udid)
    _simctl("uninstall", udid, bundle_id)


def list_apps(udid: str) -> list[dict]:
    _ensure_booted(udid)
    """Return list of installed apps with bundle ID and display name."""
    out = _simctl("listapps", udid, capture=True)
    # listapps outputs a plist; parse with plutil
    result = subprocess.run(
        ["plutil", "-convert", "json", "-o", "-", "-"],
        input=out.encode() if out else b"",
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout:
        return []
    data = json.loads(result.stdout)
    apps = []
    for bundle_id, info in data.items():
        apps.append({
            "bundle_id": bundle_id,
            "name": info.get("CFBundleDisplayName") or info.get("CFBundleName", ""),
            "version": info.get("CFBundleShortVersionString", ""),
            "path": info.get("Path", ""),
        })
    return sorted(apps, key=lambda x: x["name"].lower())


def screenshot(udid: str, output_path: str, fmt: Optional[str] = None,
               max_size: Optional[int] = None) -> None:
    """Take a screenshot. fmt: png (default), jpeg, tiff, bmp, gif.
    max_size: if set, resize so the longest dimension is ≤ max_size px (uses sips).
    """
    _ensure_booted(udid)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = ["io", udid, "screenshot"]
    if fmt:
        cmd += ["--type", fmt]
    cmd.append(output_path)
    _simctl(*cmd)
    if max_size:
        subprocess.run(["sips", "-Z", str(max_size), output_path],
                       capture_output=True, check=False)


def record_start(udid: str, output_path: str, codec: Optional[str] = None) -> int:
    _ensure_booted(udid)
    """
    Start video recording in the background. Returns PID.
    codec: hevc (default), h264, hevc-alpha
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = ["xcrun", "simctl", "io", udid, "recordVideo", "--force"]
    if codec:
        cmd += ["--codec", codec]
    cmd.append(output_path)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.pid


def record_stop(pid: int) -> None:
    """Stop a background recording process (SIGINT triggers graceful finalize)."""
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        pass


def log_stream(udid: str, predicate: Optional[str] = None, level: str = "debug") -> None:
    _ensure_booted(udid)
    """Stream logs from the simulator (blocking, Ctrl-C to stop)."""
    cmd = ["xcrun", "simctl", "spawn", udid, "log", "stream", "--level", level]
    if predicate:
        cmd += ["--predicate", predicate]
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass


def open_url(udid: str, url: str) -> None:
    _ensure_booted(udid)
    _simctl("openurl", udid, url)


def erase(udid: str) -> None:
    """Factory reset the simulator (must be shut down first)."""
    shutdown(udid)
    _simctl("erase", udid)


def delete(udid: str) -> None:
    """Permanently remove a simulator."""
    shutdown(udid)
    _simctl("delete", udid)


def rename(udid: str, new_name: str) -> None:
    """Rename a simulator."""
    _simctl("rename", udid, new_name)


def push_notification(udid: str, bundle_id: str, payload_path: str) -> None:
    _ensure_booted(udid)
    path = Path(payload_path)
    if not path.exists():
        raise RuntimeError(f"Payload file not found: {payload_path}")
    _simctl("push", udid, bundle_id, str(path))


def add_media(udid: str, file_path: str) -> None:
    _ensure_booted(udid)
    path = Path(file_path)
    if not path.exists():
        raise RuntimeError(f"File not found: {file_path}")
    _simctl("addmedia", udid, str(path))


def get_env(udid: str) -> dict:
    out = subprocess.check_output(
        ["xcrun", "simctl", "list", "devices", "--json"],
        stderr=subprocess.DEVNULL,
    )
    data = json.loads(out)
    for runtime, devices in data["devices"].items():
        for dev in devices:
            if dev["udid"] == udid:
                device_w, device_h = _get_device_logical_size(dev["name"])
                return {
                    "udid": udid,
                    "name": dev["name"],
                    "state": dev["state"],
                    "runtime": runtime.split(".")[-1].replace("-", "."),
                    "platform": "ios",
                    "screen_width_pt": device_w,
                    "screen_height_pt": device_h,
                }
    return {"udid": udid, "platform": "ios"}


def set_appearance(udid: str, mode: str) -> None:
    _ensure_booted(udid)
    """Set light or dark mode. mode must be 'light' or 'dark'."""
    _simctl("ui", udid, "appearance", mode)


def shake(udid: str) -> None:
    _ensure_booted(udid)
    """Send a shake gesture (triggers React Native dev menu)."""
    _simctl("io", udid, "shake")


def clipboard_get(udid: str) -> str:
    """Return the current contents of the simulator's pasteboard as a string.

    Useful for verifying that 'copy' buttons actually copied the expected value.
    Returns empty string if the pasteboard is empty or contains non-text data.
    """
    _ensure_booted(udid)
    import base64 as _b64
    result = subprocess.run(
        ["xcrun", "simctl", "pasteboard", "get", udid],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        return ""
    raw = result.stdout.strip()
    if not raw:
        return ""
    try:
        return _b64.b64decode(raw).decode("utf-8", errors="replace")
    except Exception:
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw


def input_text(udid: str, text: str) -> None:
    _ensure_booted(udid)
    """Paste text into the simulator via the pasteboard (works in any focused text field)."""
    import subprocess as _sp
    # Write to the simulator's pasteboard, then trigger paste
    proc = _sp.run(
        ["xcrun", "simctl", "pasteboard", "set", udid],
        input=text.encode(),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to set pasteboard: {proc.stderr.decode().strip()}")


def privacy(udid: str, bundle_id: str, action: str, service: str) -> None:
    _ensure_booted(udid)
    """
    Grant or revoke a privacy permission for an app.
    action: 'grant' | 'revoke' | 'reset'
    service: photos, camera, microphone, contacts, location, notifications, etc.
    """
    _simctl("privacy", udid, action, bundle_id, service)


def location(udid: str, lat: float, lng: float) -> None:
    _ensure_booted(udid)
    """Set a fixed GPS location on the simulator (requires Xcode 14.3+)."""
    _simctl("location", udid, "set", f"{lat},{lng}")


def location_clear(udid: str) -> None:
    """Clear the fixed GPS location override."""
    _simctl("location", udid, "clear")


# Canonical logical point dimensions (portrait) for known iOS devices.
# These are the coordinate space agents use when calling tap/swipe.
# Keyed by the device-type token in the simemu naming convention.
# Fallback: (390, 844) — standard iPhone 14/15/16 size.
_IOS_DEVICE_LOGICAL_SIZE: dict[str, tuple[int, int]] = {
    # iPhone 16 family
    "iPhone16ProMax":  (440, 956),
    "iPhone16Plus":    (430, 932),
    "iPhone16Pro":     (402, 874),
    "iPhone16":        (390, 844),
    # iPhone 15 family
    "iPhone15ProMax":  (430, 932),
    "iPhone15Plus":    (430, 932),
    "iPhone15Pro":     (393, 852),
    "iPhone15":        (393, 852),
    # iPhone 14 family
    "iPhone14ProMax":  (430, 932),
    "iPhone14Plus":    (428, 926),
    "iPhone14Pro":     (393, 852),
    "iPhone14":        (390, 844),
    # iPhone SE
    "iPhoneSE":        (375, 667),
    # iPad (common sizes — landscape has these swapped, portrait is canonical)
    "iPadPro13":       (1024, 1366),
    "iPadPro12":       (1024, 1366),
    "iPadPro11":       (834, 1194),
    "iPadAir13":       (1024, 1366),
    "iPadAir11":       (820, 1180),
    "iPadMini":        (744, 1133),
    "iPad":            (810, 1080),
}


def _get_device_logical_size(device_name: str) -> tuple[int, int]:
    """Return the canonical (width_pt, height_pt) for a device given its display name.

    Matches against _IOS_DEVICE_LOGICAL_SIZE by looking for known tokens in the
    device name (e.g. "Mochi iPhone16Pro 6.3in iOS18" → "iPhone16Pro" → (402, 874)).
    Falls back to (390, 844) for unknown devices.
    """
    for token, size in _IOS_DEVICE_LOGICAL_SIZE.items():
        if token.lower() in device_name.lower().replace(" ", "").replace("-", ""):
            return size
    return (390, 844)


def _get_device_name(udid: str) -> str:
    """Return the display name of a simulator by UDID."""
    import subprocess as _sp
    import json as _json
    out = _sp.run(
        ["xcrun", "simctl", "list", "devices", "--json"],
        capture_output=True, text=True, check=True,
    )
    data = _json.loads(out.stdout)
    for _runtime, devices in data["devices"].items():
        for dev in devices:
            if dev["udid"] == udid:
                return dev["name"]
    raise RuntimeError(f"Simulator {udid} not found in simctl list")


def _raise_sim_window(device_name: str) -> None:
    """Raise the Simulator window to the front of z-order without activating the app.

    This lets CGEventPost deliver clicks to the Simulator window without
    changing which app has keyboard focus or moving the user's cursor.
    """
    import subprocess as _sp
    _sp.run(["osascript", "-e", f'''tell application "System Events"
    tell process "Simulator"
        perform action "AXRaise" of (first window whose name contains "{device_name}")
    end tell
end tell'''], capture_output=True, check=False)


def _get_sim_bounds(udid: str) -> tuple[float, float, float, float]:
    """Return (px, py, sw, sh) — screen origin and pixel size of the simulator content area."""
    import subprocess as _sp

    device_name = _get_device_name(udid)

    r = _sp.run([
        "osascript", "-e",
        f'''tell application "System Events"
    tell process "Simulator"
        set w to first window whose name contains "{device_name}"
        set grp to first UI element of w whose role is "AXGroup"
        set p to position of grp
        set s to size of grp
        return ((item 1 of p) as string) & "," & ((item 2 of p) as string) & "," & ((item 1 of s) as string) & "," & ((item 2 of s) as string)
    end tell
end tell''',
    ], capture_output=True, text=True, check=False)

    bounds_str = r.stdout.strip()
    if not bounds_str or "," not in bounds_str:
        raise RuntimeError(f"Could not get device content bounds for simulator {udid}")

    parts = [float(v.strip()) for v in bounds_str.split(",")]
    return parts[0], parts[1], parts[2], parts[3]


def _logical_to_screen(
    lx: int, ly: int,
    px: float, py: float,
    sw: float, sh: float,
    device_w: int = 390, device_h: int = 844,
) -> tuple[int, int]:
    """Convert logical points to Mac screen coordinates.

    lx, ly      — input in the device's logical point space (e.g. 402×874 for iPhone 16 Pro)
    px, py      — top-left of the simulator content area on the Mac display
    sw, sh      — displayed size of the simulator content area in Mac display points
    device_w/h  — canonical logical size of the device (from _get_device_logical_size)

    At 100% zoom sw == device_w, so the scale factor is 1.0.
    At other zoom levels sw/device_w gives the correct scale.
    """
    scale_x = sw / device_w
    scale_y = sh / device_h
    return int(px + lx * scale_x), int(py + ly * scale_y)


# macOS virtual key codes used by Simulator.app shortcuts
_VK_H = 4    # h
_VK_L = 37   # l
_VK_S = 1    # s
_VK_LEFT  = 123
_VK_RIGHT = 124

def _run_system_events(script: str) -> None:
    """Execute a small System Events script, ignoring AppleScript UI noise."""
    subprocess.run(["osascript", "-e", script], capture_output=True, check=False)


def _post_key(vk: int, modifiers: tuple[str, ...] = ()) -> None:
    """Send a Simulator keyboard shortcut via System Events.

    CGEvent keyboard injection has become a silent no-op on recent macOS
    setups. System Events is slower, but it reliably reaches Simulator.
    """
    using_clause = ""
    if modifiers:
        using_clause = " using {" + ", ".join(modifiers) + "}"
    _run_system_events(f'''
tell application "System Events"
    tell process "Simulator"
        set frontmost to true
        key code {vk}{using_clause}
    end tell
end tell''')


def _click_simulator_at(x: int, y: int) -> None:
    """Click global screen coordinates inside the Simulator process."""
    _run_system_events(f'''
tell application "System Events"
    tell process "Simulator"
        click at {{{x}, {y}}}
    end tell
end tell''')


def _pct_string(x: int, y: int, width: int, height: int) -> str:
    """Convert logical coordinates to Maestro percentage syntax."""
    px = max(0.0, min(100.0, (x / width) * 100.0))
    py = max(0.0, min(100.0, (y / height) * 100.0))
    return f"{px:.2f}%, {py:.2f}%"


def _run_maestro_flow(udid: str, commands: str) -> None:
    """Run a small Maestro flow against the current foreground iOS app."""
    if not shutil.which("maestro"):
        raise RuntimeError("maestro is not installed or not on PATH")

    flow = f"""appId: com.apple.springboard
---
{commands}"""

    fd, flow_path = tempfile.mkstemp(prefix="simemu-ios-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(flow)

        result = subprocess.run(
            ["maestro", "--device", udid, "test", flow_path, "--format", "NOOP", "--no-ansi"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(detail or "maestro test failed")
    finally:
        try:
            os.unlink(flow_path)
        except FileNotFoundError:
            pass


def _post_mouse_hidden(events_fn) -> None:
    """Run events_fn(Quartz) with the system cursor hidden.

    Saves the cursor position, hides the cursor, calls events_fn (which may
    move the cursor via CGEventPost), warps back to the original position, then
    shows the cursor. The user never sees the cursor move.
    """
    import importlib as _il
    Quartz = _il.import_module("Quartz")

    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    orig = Quartz.CGPoint(x=loc.x, y=loc.y)
    # Newer PyObjC bindings may omit kCGDirectMainDisplay while still exposing
    # CGMainDisplayID(). Resolve the display ID dynamically so tap/swipe/press
    # continue to work across Quartz binding versions.
    if hasattr(Quartz, "CGMainDisplayID"):
        display_id = Quartz.CGMainDisplayID()
    else:
        display_id = Quartz.kCGDirectMainDisplay
    Quartz.CGDisplayHideCursor(display_id)
    try:
        events_fn(Quartz)
        Quartz.CGWarpMouseCursorPosition(orig)
    finally:
        Quartz.CGDisplayShowCursor(display_id)


def tap(udid: str, x: int, y: int) -> None:
    """Tap at logical-point coordinates on the simulator screen.

    Raises the Simulator window and sends a System Events click at the mapped
    global screen coordinate. This is more reliable than CGEventPost-based
    mouse injection on current macOS/PyObjC setups.
    """
    _ensure_booted(udid)
    device_name = _get_device_name(udid)
    device_w, device_h = _get_device_logical_size(device_name)
    px, py, sw, sh = _get_sim_bounds(udid)
    cx, cy = _logical_to_screen(x, y, px, py, sw, sh, device_w, device_h)
    _raise_sim_window(device_name)
    _click_simulator_at(cx, cy)


def _swipe_quartz(udid: str, x1: int, y1: int, x2: int, y2: int, duration: float) -> None:
    """Fallback swipe backend using Quartz mouse drag events."""
    import importlib as _il
    Quartz = _il.import_module("Quartz")
    import time as _t

    _ensure_booted(udid)
    device_name = _get_device_name(udid)
    device_w, device_h = _get_device_logical_size(device_name)
    px, py, sw, sh = _get_sim_bounds(udid)
    _raise_sim_window(device_name)

    sx1, sy1 = _logical_to_screen(x1, y1, px, py, sw, sh, device_w, device_h)
    sx2, sy2 = _logical_to_screen(x2, y2, px, py, sw, sh, device_w, device_h)
    steps = max(10, int(duration * 60))
    step_delay = duration / steps

    def _drag(Q):
        src = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
        start = Q.CGPoint(x=sx1, y=sy1)
        e_dn = Q.CGEventCreateMouseEvent(src, Q.kCGEventLeftMouseDown, start, Q.kCGMouseButtonLeft)
        Q.CGEventPost(Q.kCGSessionEventTap, e_dn)
        _t.sleep(step_delay)
        for i in range(1, steps + 1):
            t = i / steps
            pos = Q.CGPoint(x=int(sx1 + (sx2 - sx1) * t), y=int(sy1 + (sy2 - sy1) * t))
            e_drag = Q.CGEventCreateMouseEvent(src, Q.kCGEventLeftMouseDragged, pos, Q.kCGMouseButtonLeft)
            Q.CGEventPost(Q.kCGSessionEventTap, e_drag)
            _t.sleep(step_delay)
        end = Q.CGPoint(x=sx2, y=sy2)
        e_up = Q.CGEventCreateMouseEvent(src, Q.kCGEventLeftMouseUp, end, Q.kCGMouseButtonLeft)
        Q.CGEventPost(Q.kCGSessionEventTap, e_up)

    _post_mouse_hidden(_drag)


def swipe(udid: str, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> None:
    """Swipe from (x1,y1) to (x2,y2) in logical points over duration seconds.

    Maestro is the preferred backend because it is materially more reliable on
    current macOS/iOS Simulator setups. Quartz remains as a fallback.
    """
    _ensure_booted(udid)
    device_name = _get_device_name(udid)
    device_w, device_h = _get_device_logical_size(device_name)
    _raise_sim_window(device_name)

    try:
        _run_maestro_flow(
            udid,
            "\n".join([
                "- swipe:",
                f'    start: "{_pct_string(x1, y1, device_w, device_h)}"',
                f'    end: "{_pct_string(x2, y2, device_w, device_h)}"',
                f"    duration: {int(duration * 1000)}",
            ]),
        )
    except Exception:
        _swipe_quartz(udid, x1, y1, x2, y2, duration)


def _long_press_quartz(udid: str, x: int, y: int, duration: float) -> None:
    """Fallback long-press backend using Quartz mouse events."""
    import importlib as _il
    Quartz = _il.import_module("Quartz")
    import time as _t

    _ensure_booted(udid)
    device_name = _get_device_name(udid)
    device_w, device_h = _get_device_logical_size(device_name)
    px, py, sw, sh = _get_sim_bounds(udid)
    cx, cy = _logical_to_screen(x, y, px, py, sw, sh, device_w, device_h)
    _raise_sim_window(device_name)

    def _press(Q):
        src = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
        pos = Q.CGPoint(x=cx, y=cy)
        e_dn = Q.CGEventCreateMouseEvent(src, Q.kCGEventLeftMouseDown, pos, Q.kCGMouseButtonLeft)
        e_up = Q.CGEventCreateMouseEvent(src, Q.kCGEventLeftMouseUp,   pos, Q.kCGMouseButtonLeft)
        Q.CGEventPost(Q.kCGSessionEventTap, e_dn)
        _t.sleep(duration)
        Q.CGEventPost(Q.kCGSessionEventTap, e_up)

    _post_mouse_hidden(_press)


def long_press(udid: str, x: int, y: int, duration: float = 1.0) -> None:
    """Long-press at a logical-point coordinate. duration in seconds (default 1.0).

    Maestro is used for the default press because it is more reliable than
    Quartz-based mouse injection. Custom durations fall back to Quartz.
    """
    _ensure_booted(udid)
    device_name = _get_device_name(udid)
    device_w, device_h = _get_device_logical_size(device_name)
    _raise_sim_window(device_name)

    if abs(duration - 1.0) > 0.05:
        _long_press_quartz(udid, x, y, duration)
        return

    try:
        _run_maestro_flow(
            udid,
            "\n".join([
                "- longPressOn:",
                f'    point: "{_pct_string(x, y, device_w, device_h)}"',
            ]),
        )
    except Exception:
        _long_press_quartz(udid, x, y, duration)


def rotate(udid: str, orientation: str) -> None:
    """Set device orientation: 'portrait', 'landscape', 'left', or 'right'.

    'portrait' and 'landscape' check the current state and only rotate if needed.
    'left' / 'right' always send one rotation in that direction.
    May bring Simulator to the front while sending the shortcut.
    """
    _ensure_booted(udid)
    orientation = orientation.lower()

    if orientation in ("left", "right"):
        vk = _VK_LEFT if orientation == "left" else _VK_RIGHT
        _post_key(vk, ("command down",))
        return

    if orientation not in ("portrait", "landscape"):
        raise RuntimeError(f"orientation must be portrait, landscape, left, or right — got '{orientation}'")

    # Check current orientation from window dimensions and rotate once if needed
    px, py, sw, sh = _get_sim_bounds(udid)
    current = "landscape" if sw > sh else "portrait"
    if current != orientation:
        _post_key(_VK_RIGHT, ("command down",))


# Named key → (virtual key code, modifier names, description)
_VK_V = 9   # v

_IOS_KEYS: dict[str, tuple[int, tuple[str, ...], str]] = {
    "home":       (_VK_H, ("command down", "shift down"), "Go to home screen (Cmd+Shift+H)"),
    "lock":       (_VK_L, ("command down",), "Lock device (Cmd+L)"),
    "siri":       (_VK_H, ("command down",), "Invoke Siri (Cmd+H)"),
    "screenshot": (_VK_S, ("command down", "shift down"), "System screenshot (Cmd+Shift+S)"),
    "paste":      (_VK_V, ("command down",), "Paste clipboard (Cmd+V)"),
}


def key(udid: str, key_name: str) -> None:
    """Press a named hardware key on the simulator.

    Supported: home, lock, siri, screenshot
    May bring Simulator to the front while sending the shortcut.
    """
    _ensure_booted(udid)
    k = key_name.lower()
    if k not in _IOS_KEYS:
        raise RuntimeError(
            f"Unknown iOS key '{key_name}'. Supported: {', '.join(_IOS_KEYS)}"
        )
    vk, modifiers, _ = _IOS_KEYS[k]
    _post_key(vk, modifiers)


def status_bar(udid: str, time_str: Optional[str] = None, battery: Optional[int] = None,
               wifi: Optional[int] = None, network: Optional[str] = None) -> None:
    """Override the simulator status bar for clean screenshots.

    time_str: clock display, e.g. "9:41"
    battery:  0-100
    wifi:     0-3 bars
    network:  wifi | 5g | 4g | lte | 3g | 2g | edge | none
    """
    _ensure_booted(udid)
    cmd = ["status_bar", udid, "override"]
    if time_str:
        cmd += ["--time", time_str]
    if battery is not None:
        cmd += ["--batteryLevel", str(battery), "--batteryState", "charged"]
    if wifi is not None:
        cmd += ["--wifiBars", str(min(3, max(0, wifi)))]
    if network:
        cmd += ["--dataNetwork", network]
    _simctl(*cmd)


def status_bar_clear(udid: str) -> None:
    """Restore the real status bar after an override."""
    _ensure_booted(udid)
    _simctl("status_bar", udid, "clear")


def set_animations(udid: str, enabled: bool) -> None:
    """Enable or disable UI animations.

    disabled (enabled=False): turns on slow-animations mode via simctl io — makes
    animations deterministic and Maestro flows more stable.
    enabled (enabled=True): restores normal animation speed.
    """
    _ensure_booted(udid)
    flag = "off" if enabled else "on"   # simctl: 'on' = slow animations enabled
    _simctl("io", udid, "setSlowAnimations", flag)


def focus(udid: str) -> None:
    """Bring the Simulator window to the front and activate the app so the user can see it."""
    import subprocess as _sp
    device_name = _get_device_name(udid)
    _sp.run(["osascript", "-e", f'''tell application "Simulator" to activate
tell application "System Events"
    tell process "Simulator"
        perform action "AXRaise" of (first window whose name contains "{device_name}")
    end tell
end tell'''], capture_output=True, check=False)


def reboot(udid: str) -> None:
    """Reboot the simulator (shutdown + boot)."""
    shutdown(udid)
    import time
    time.sleep(2)
    boot(udid)


def reset_app(udid: str, bundle_id: str) -> None:
    """Force-stop, clear all app data, then relaunch. iOS equivalent of Android clear-data + restart.

    Uses `xcrun simctl privacy reset` to clear permissions, then uninstalls/reinstalls
    is NOT done — instead we use the container reset approach via simctl.
    For a full data wipe, use: simemu erase <slug> (wipes entire simulator).
    This resets the app's data container (Documents, Library, tmp, UserDefaults).
    """
    _ensure_booted(udid)
    # terminate first so the app doesn't fight the container deletion
    _simctl("terminate", udid, bundle_id, check=False)
    import time as _t
    _t.sleep(0.5)
    # delete the app's data container — simctl respawns it clean on next launch
    _simctl("privacy", udid, "reset", "all", bundle_id, check=False)
    result = subprocess.run(
        ["xcrun", "simctl", "get_app_container", udid, bundle_id, "data"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        data_container = result.stdout.strip()
        if data_container:
            import shutil as _sh
            _sh.rmtree(data_container, ignore_errors=True)
    _simctl("launch", udid, bundle_id)


def crash_log(udid: str, bundle_id: Optional[str] = None, since_minutes: int = 60) -> Optional[str]:
    """Return the most recent crash report for the simulator (or a specific app).

    Searches ~/Library/Logs/DiagnosticReports for .crash or .ips files modified
    within the last since_minutes minutes. Returns the raw crash text, or None if
    no recent crashes found.
    """
    import glob as _glob
    import time as _t

    diag = Path.home() / "Library" / "Logs" / "DiagnosticReports"
    if not diag.exists():
        return None

    cutoff = _t.time() - since_minutes * 60
    candidates = []
    for pattern in ("*.crash", "*.ips"):
        for p in diag.glob(pattern):
            if p.stat().st_mtime >= cutoff:
                candidates.append(p)

    if bundle_id:
        # filter to files whose name starts with the app name (bundle_id last component)
        app_name = bundle_id.split(".")[-1].lower()
        candidates = [p for p in candidates if app_name in p.name.lower()]

    if not candidates:
        return None

    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return newest.read_text(errors="replace")


def biometrics(udid: str, match: bool) -> None:
    """Simulate a Face ID / Touch ID result.

    match=True  → successful authentication
    match=False → failed authentication

    The device must have biometrics enrolled first:
    Simulator → Features → Face ID / Touch ID → Enrolled
    """
    import subprocess as _sp

    _ensure_booted(udid)
    face_action  = "Matching Face"  if match else "Non-Matching Face"
    touch_action = "Matching Touch" if match else "Non-Matching Touch"
    _sp.run(["osascript", "-e", f'''
tell application "System Events"
    tell process "Simulator"
        set frontmost to true
        try
            click menu item "{face_action}" of menu 1 of menu item "Face ID" of menu "Features" of menu bar 1
        on error
            click menu item "{touch_action}" of menu 1 of menu item "Touch ID" of menu "Features" of menu bar 1
        end try
    end tell
end tell'''], capture_output=True, check=False)
