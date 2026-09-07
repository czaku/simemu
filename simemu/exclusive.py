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
import time
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
# Shell flags that consume a following operand (the option name) rather than
# being self-contained — that operand must be skipped, not mistaken for the
# first positional argument, by _looks_like_one_shot_shell.
_OPTIONS_WITH_OPERAND = frozenset({"-o", "+o", "-O", "+O"})


_PS_RETRY_ATTEMPTS = 3
_PS_RETRY_DELAY_SECONDS = 0.05


def _ppid_and_command(pid: int) -> tuple[int | None, str | None]:
    """Read `pid`'s parent PID and full command line from a SINGLE `ps` call.

    Two separate `ps` invocations (one for ppid, one for command) leave a
    TOCTOU window between them in which `pid` could exit and its slot get
    reused by an unrelated process before the second call runs, making the
    two fields describe two different processes. One call over both fields
    is an atomic kernel snapshot — it either describes one real process or
    fails outright; it cannot describe two.

    Retries a bounded number of times ONLY when `ps` itself could not be run
    (spawn failure, timeout) — a transient hiccup (e.g. a momentarily
    overloaded box, exactly the "long xcodebuild eating CPU" scenario this
    whole subsystem exists to tolerate). Does NOT retry a clean "no such
    process" result (non-zero exit with `ps` running fine): that is `ps`
    authoritatively reporting the pid is gone, not a hiccup — retrying
    wouldn't change that answer, only delay reporting it.
    """
    for attempt in range(_PS_RETRY_ATTEMPTS):
        try:
            result = subprocess.run(
                ["ps", "-o", "ppid=,command=", "-p", str(pid)],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            if attempt + 1 < _PS_RETRY_ATTEMPTS:
                time.sleep(_PS_RETRY_DELAY_SECONDS)
                continue
            return None, None
        break
    if result.returncode != 0:
        return None, None
    line = result.stdout.strip("\n")
    if not line.strip():
        return None, None
    # Leading field is PPID (no internal spaces); command is everything after
    # the first run of whitespace, however many spaces/args it contains.
    parts = line.strip().split(None, 1)
    if not parts:
        return None, None
    try:
        ppid = int(parts[0])
    except ValueError:
        return None, None
    command = parts[1] if len(parts) > 1 else ""
    return (ppid if ppid > 1 else None), (command or None)


def _looks_like_one_shot_shell(command_line: str) -> bool:
    """True when `command_line` is a shell invoked to run one command and exit.

    Matches `zsh -c '...'`, `/bin/bash -lc "..."`, `sh -c ...`, etc. — any
    invocation of a known shell with a `-c`-style flag (including combined
    flags like `-lc`), PROVIDED that flag appears before the first positional
    argument. Long-form options (`--foo`, e.g. `--login`) are ignored since
    none of these shells use `--command`, and scanning continues past them —
    but a BARE `--` is the POSIX end-of-options marker: bash/zsh/ksh treat
    anything after it as a positional argument (a script/file name), so a
    literal `-c` appearing after `--` is that filename, not the flag, and
    scanning stops there. bash/zsh/ksh also accept `+X` as the toggle-OFF
    form of any `-X` single-letter option (e.g. `+e` disables errexit, the
    mirror of `-e`) — these are flags too, just like their `-X` counterparts,
    not positional arguments, even though they don't start with `-`. `-o`/
    `+o`/`-O`/`+O` (set a named shell/shopt option, e.g. `-o errexit`) take a
    following operand that is NOT itself a flag — it's skipped rather than
    treated as the first positional argument, so a real `-c` later in the
    same invocation (e.g. `bash -o errexit -c '...'` or `bash +e -c '...'`)
    is still found. Scanning otherwise STOPS at the first genuine positional
    argument (a token starting with neither `-` nor `+`), since everything
    after that is an argument to a script/command, not a flag to the shell
    itself — without this, `bash build.sh -c` (a durable script run, whose
    OWN arg happens to be `-c`) would be misread as a one-shot `-c`
    invocation of bash itself. An interactive or login shell with no `-c`
    flag (a human's terminal, a persistent script shell) never matches, so
    behavior for those callers is unchanged.
    """
    tokens = command_line.split()
    if not tokens:
        return False
    exe = tokens[0].rsplit("/", 1)[-1]
    if exe not in _ONE_SHOT_SHELL_NAMES:
        return False
    args = tokens[1:]
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            # End-of-options marker — nothing after this is a shell flag.
            break
        if tok in _OPTIONS_WITH_OPERAND:
            # Takes a following operand (e.g. "-o errexit") that is itself
            # not a flag — skip it rather than treating it as the first
            # positional argument.
            i += 2
            continue
        if tok == "-" or not (tok.startswith("-") or tok.startswith("+")):
            # A bare "-" (read stdin) or a plain positional argument (a
            # script path, or the first word of what -c already matched) —
            # nothing past this point is a flag to the shell itself.
            break
        if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
            # Only the "-"-prefixed form carries -c ("run this command and
            # exit") — there is no "+c" equivalent in any of these shells.
            return True
        i += 1
    return False


def _find_durable_ancestor(start_pid: int) -> int:
    """Walk up from `start_pid`, skipping one-shot '<shell> -c ...' wrappers.

    Lands on the nearest ancestor that is NOT itself a transient per-command
    wrapper — in practice, the agent's own persistent CLI/harness process, an
    orchestrator, or an interactive shell — which is a much closer proxy for
    "is the holder of this claim still around" than the immediate parent.

    Only ever returns a pid whose command line this function itself actually
    read and confirmed durable in THIS same hop, or `start_pid` (the pre-
    existing, already-shipped behavior). It never returns an unverified
    intermediate ancestor: if a probe fails, or the hop limit is reached,
    mid-walk — before a durable (non-one-shot) pid has been confirmed — the
    walk gives up and falls back to `start_pid` rather than guessing that
    whatever pid it last reached is safe. Guessing there was the bug: an
    unverified ancestor could itself be a transient wrapper that exits
    moments later, reproducing the exact premature-reap/double-claim failure
    this walk exists to prevent.
    """
    pid = start_pid
    for _ in range(_MAX_ANCESTOR_HOPS):
        parent, command = _ppid_and_command(pid)
        if command is None:
            return start_pid
        if not _looks_like_one_shot_shell(command):
            return pid
        if parent is None or parent == pid:
            return start_pid
        pid = parent
    return start_pid


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
