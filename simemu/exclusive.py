"""
Exclusive-claim primitives for simemu sessions.

A claim is exclusive when the device's sim_id appears in exactly one non-terminal
session at a time. The session store (`sessions.json`) is already protected by
`fcntl.flock` — this module adds two pieces on top:

1. Liveness tracking: each session records the PID and process-group of the
   claimant. A claim whose owner process is no longer alive is "stale" and
   may be reaped by the next claim attempt.

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
import subprocess
from typing import Iterable

# T-LU-054 / T-054: an agent almost never invokes `simemu` from a shell that
# outlives a single command. Each tool call typically spawns a FRESH
# `<shell> -c "simemu ..."` wrapper that execs simemu and exits the instant
# that one command returns — regardless of whether the agent's actual task
# is still running. os.getppid() lands on that wrapper, which is stale within
# a fraction of a second, so a liveness check anchored there reads "dead"
# almost continuously even while the real holder (the agent, or a long build
# it kicked off in a separate call) is still very much active. This is the
# confirmed root cause of claims being reaped mid-cycle, sometimes multiple
# times within the same minute. See _find_durable_ancestor() below.
_MAX_ANCESTOR_HOPS = 8
_ONE_SHOT_SHELL_NAMES = frozenset({"zsh", "bash", "sh", "dash", "ksh"})


def _ps_field(pid: int, keyword: str) -> str | None:
    """Return one `ps` output field for `pid`, or None if it can't be read.

    Failure (process gone, permission denied, `ps` missing) is not
    distinguished from "no useful answer" — every caller here treats None as
    "stop walking, use what we already have," which is always at least as
    safe as the pre-existing single-PID behavior.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", f"{keyword}=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _parent_pid(pid: int) -> int | None:
    raw = _ps_field(pid, "ppid")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 1 else None


def _process_command(pid: int) -> str | None:
    """Return the full command line (argv, space-joined) for `pid`."""
    return _ps_field(pid, "command")


def _looks_like_one_shot_shell(command_line: str) -> bool:
    """True when `command_line` is a shell invoked to run one command and exit.

    Matches `zsh -c '...'`, `/bin/bash -lc "..."`, `sh -c ...`, etc. — any
    invocation of a known shell with a `-c`-style flag (including combined
    flags like `-lc`). Long-form options (`--foo`) are ignored since none of
    these shells use `--command`. An interactive or login shell with no `-c`
    flag (a human's terminal, a persistent script shell) never matches, so
    behavior for those callers is unchanged.
    """
    tokens = command_line.split()
    if not tokens:
        return False
    exe = tokens[0].rsplit("/", 1)[-1]
    if exe not in _ONE_SHOT_SHELL_NAMES:
        return False
    for tok in tokens[1:]:
        if not tok.startswith("-") or tok.startswith("--"):
            continue
        if tok == "-":
            continue
        if "c" in tok[1:]:
            return True
    return False


def _find_durable_ancestor(start_pid: int) -> int:
    """Walk up from `start_pid`, skipping one-shot '<shell> -c ...' wrappers.

    Lands on the nearest ancestor that is NOT itself a transient per-command
    wrapper — in practice, the agent's own persistent CLI/harness process, an
    orchestrator, or an interactive shell — which is a much closer proxy for
    "is the holder of this claim still around" than the immediate parent.
    Falls back to `start_pid` the moment `ps` can't answer, which reproduces
    the pre-existing (already-shipped) behavior exactly — this never makes
    liveness tracking less safe than it was before, only less trigger-happy.
    """
    pid = start_pid
    for _ in range(_MAX_ANCESTOR_HOPS):
        command = _process_command(pid)
        if command is None or not _looks_like_one_shot_shell(command):
            return pid
        parent = _parent_pid(pid)
        if parent is None or parent == pid:
            return pid
        pid = parent
    return pid


def claimant_pid() -> int:
    """Return the PID that should be recorded as the claim owner.

    The simemu CLI is typically a short-lived process: an agent shell invokes
    `simemu claim ios`, the CLI prints the session JSON and exits, and
    whatever kept that invocation alive keeps the session alive across many
    follow-up `simemu do` calls. If we recorded the CLI's own PID
    (os.getpid()), every claim would look "stale" the moment the CLI exited,
    and a sibling claim attempt would reap it — re-freeing the device for
    double-claim. That is exactly the incident this module exists to
    prevent.

    Resolution order:

    1. ``SIMEMU_CLAIMANT_PID`` — explicit override from a long-lived
       supervisor (e.g. an orchestrator that wants to bind the claim to its
       own PID).
    2. ``os.getppid()``, walked up past any one-shot `<shell> -c ...`
       wrapper (see `_find_durable_ancestor`) to the nearest ancestor that
       actually survives between commands.
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
            return _find_durable_ancestor(ppid)
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


def collect_stale_session_ids(sessions: dict) -> list[str]:
    """Return session_ids whose claim PID is no longer alive.

    Only considers sessions in non-terminal status (active/idle/parked). Sessions
    without a recorded `pid` (legacy entries created before this field existed)
    are NOT considered stale on PID grounds alone — they fall back to the
    existing heartbeat/expiry logic in session.py.
    """
    stale: list[str] = []
    for sid, raw in sessions.items():
        if raw.get("status") in ("expired", "released"):
            continue
        pid = raw.get("pid")
        if pid is None:
            continue
        if not is_pid_alive(pid):
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
