# Isolation footgun + PID-tied session liveness (runtime-observed 2026-06-25)

## SIMEMU_OUTPUT_DIR does not isolate state/sessions (P2, REAL)
cli.py module docstring: "Set SIMEMU_OUTPUT_DIR to override the default output directory (~/.simemu/)".
Reality: the session/state store uses state_dir() = SIMEMU_STATE_DIR (state.py:33); config uses
SIMEMU_CONFIG_DIR (state.py:37). SIMEMU_OUTPUT_DIR only affects screenshot/output file paths.
PROOF: ran `SIMEMU_OUTPUT_DIR=/tmp/simemu-steal-78758 simemu claim ios` -> created
s-8a4c3b in GLOBAL ~/.simemu/sessions.json (isolated /tmp dir stayed empty; ~/.simemu/sessions.json
present and mutated). A user/agent setting SIMEMU_OUTPUT_DIR expecting a sandbox still shares the
global session store -> cross-contamination between supposedly-isolated agents/projects.
FIX: make the session/state store honor SIMEMU_OUTPUT_DIR too, or correct the docstring/docs and
provide a single documented isolation var.

## Session liveness tied to claiming PID; reaped when claim process exits (P1, investigate)
Observed: agentA `simemu claim` returned s-8a4c3b; on the very next invocation the reaper logged
"Reaped session 's-...' — claimant PID dead" and a follow-up `do s-8a4c3b env` returned a JSON of
all-None values with exit 0 (not a 'session not found' error). Because each `simemu` call is a
short-lived process, if `pid` records the claim process it dies instantly -> session vulnerable to
immediate reaping. Needs confirmation of what pid is recorded (claim pid vs stable parent) and the
PID-reuse false-alive race. (alloc-correctness agent owns the clean isolated repro.)

## `do <dead/unknown session> env` returns nulls + exit 0 (P2, REAL-ish)
A released/reaped session id fed to `do ... env` returned {slug:null,device_name:null,...} exit 0
instead of a clean session_expired/not_found error. Masks failures from agents.

## Session enumeration open + no-auth do (P0/P1) — see session-stealing.md

## sessions.json grows unboundedly — dead records never pruned (P2, REAL)
Real ~/.simemu/sessions.json holds 51 session records; `simemu sessions --json` shows 1 active.
Reaper marks dead sessions released but does not delete the record from disk -> file grows forever
(74 KB here). No `simemu sessions --prune`. Long-running multi-agent host accumulates thousands.
(Note: this teardown added 1 inert released record, s-8a4c3b, during the steal test; left in place
to avoid racing a write on the user's live file.)
FIX: prune released/expired records older than N hours on write, or add a prune command + cap.
