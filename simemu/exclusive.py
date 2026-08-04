"""
Exclusive-claim primitives for simemu sessions.

A claim is exclusive when the device's sim_id appears in exactly one non-terminal
session at a time. The session store (`sessions.json`) is already protected by
`fcntl.flock` — this module adds two pieces on top:

1. Liveness tracking: each session records the PID and process-group of the
   claimant. A claim whose owner process is no longer alive is a *candidate*
   for reaping by the next claim attempt — but a dead claimant alone is NOT
   sufficient. The device must also be idle (no running process references
   its UDID); see `device_in_use`. Reaping on PID death alone frees devices
   that are still under active `xcodebuild` test runs.

2. Token-based ownership: each session is issued a `claim_token` (opaque
   secret). Callers that want to enforce strict ownership (sub-agent spawning
   the session, child shells inheriting via env) can validate the token in
   `simemu do` by setting `SIMEMU_SESSION_TOKEN`. Backwards-compatible: when
   the env var is unset the check is skipped (existing CLI callers still work).

This module is deliberately small and side-effect-free. The session module
imports its helpers; tests can call them directly.
"""

from __future__ import annotations

import os
from typing import Iterable


def claimant_pid() -> int:
    """Return the PID that should be recorded as the claim owner.

    The simemu CLI is typically a short-lived process: an agent shell invokes
    `simemu claim ios`, the CLI prints the session JSON and exits, and the
    SHELL keeps the session alive across many follow-up `simemu do` calls.
    If we recorded the CLI's own PID (os.getpid()), every claim would look
    "stale" the moment the CLI exited, and a sibling claim attempt would
    reap it — re-freeing the device for double-claim. That is exactly the
    incident this module exists to prevent.

    Resolution order:

    1. ``SIMEMU_CLAIMANT_PID`` — explicit override from a long-lived
       supervisor (e.g. an orchestrator that wants to bind the claim to its
       own PID).
    2. ``os.getppid()`` — the shell / parent process. Survives the CLI exit.
    3. ``os.getpid()`` — last-resort fallback (only when getppid fails).
    """
    env = os.environ.get("SIMEMU_CLAIMANT_PID")
    if env:
        try:
            value = int(env)
            if value > 0:
                return value
        except ValueError:
            pass
    try:
        ppid = os.getppid()
        if ppid and ppid > 1:
            return ppid
    except OSError:
        pass
    return os.getpid()


def is_pid_alive(pid: int | None) -> bool:
    """Return True if the given PID corresponds to a currently-running process.

    Returns False for None, 0, or any PID that signal(0) cannot reach. We use
    signal(0) which is the standard POSIX liveness probe — it does nothing
    except check whether the kernel still has a process slot for that PID.
    """
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user — that's "alive" for our purposes.
        return True
    except OSError:
        return False
    return True


def running_process_command_lines() -> list[str]:
    """Return the command line of every running process.

    Isolated behind its own function so `collect_stale_session_ids` stays
    testable without a real simulator: tests inject the lines directly.
    Any failure to probe returns an empty list, which is the SAFE direction
    for the caller — see `device_in_use`.
    """
    import subprocess

    try:
        completed = subprocess.run(
            ["ps", "-Ao", "command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return completed.stdout.splitlines()


def device_in_use(sim_id: str | None, command_lines: Iterable[str]) -> bool:
    """True when any running process references this device UDID.

    A claim's PID is the SHELL that ran `simemu claim` (see `claimant_pid`).
    That shell is frequently short-lived — an agent running
    `zsh -c 'simemu claim ios && xcodebuild ...'` exits the wrapper while
    `xcodebuild -destination id=<udid>` keeps driving the device for minutes
    afterwards. PID liveness therefore says nothing about whether the DEVICE
    is free, and reaping on it alone frees a device out from under live work,
    re-opening it for a sibling claim — the double-claim this module exists
    to prevent.

    Matching is a plain substring test on the UDID. That deliberately errs
    toward "in use": an unrelated process merely mentioning the UDID blocks a
    reap. Refusing to free a device is recoverable; double-claiming one is not.
    """
    if not sim_id:
        return False
    return any(sim_id in line for line in command_lines)


def collect_stale_session_ids(
    sessions: dict,
    command_lines: Iterable[str] | None = None,
) -> list[str]:
    """Return session_ids whose claim PID is dead AND whose device is idle.

    Only considers sessions in non-terminal status (active/idle/parked). Sessions
    without a recorded `pid` (legacy entries created before this field existed)
    are NOT considered stale on PID grounds alone — they fall back to the
    existing heartbeat/expiry logic in session.py.

    A dead claimant is necessary but NOT sufficient to reap. The device must
    also be idle: no running process may reference its `sim_id`. Pass
    `command_lines` to inject the process table (tests); when omitted it is
    probed once via `running_process_command_lines`.
    """
    stale: list[str] = []
    candidates: list[tuple[str, dict]] = []

    for sid, raw in sessions.items():
        if raw.get("status") in ("expired", "released"):
            continue
        pid = raw.get("pid")
        if pid is None:
            continue
        if not is_pid_alive(pid):
            candidates.append((sid, raw))

    if not candidates:
        return stale

    # Probe the process table at most once, and only when there is something
    # to protect.
    lines = list(
        command_lines if command_lines is not None else running_process_command_lines()
    )

    for sid, raw in candidates:
        if device_in_use(raw.get("sim_id"), lines):
            continue
        stale.append(sid)
    return stale


def mark_sessions_reaped(data: dict, session_ids: Iterable[str], now_iso: str) -> None:
    """Mutate `data` in place: mark each session as expired with a reap reason."""
    for sid in session_ids:
        raw = data["sessions"].get(sid)
        if raw is None:
            continue
        raw["status"] = "expired"
        raw["expires_at"] = now_iso
        raw["reaped_reason"] = "claimant_pid_dead"
        raw["reaped_at"] = now_iso


def issue_claim_token() -> str:
    """Return a fresh opaque claim token (32 hex chars / 128 bits)."""
    import secrets
    return secrets.token_hex(16)


def validate_token(expected: str | None, presented: str | None) -> bool:
    """Constant-time compare; True only when both are non-empty and equal."""
    if not expected or not presented:
        return False
    import hmac
    return hmac.compare_digest(expected, presented)
