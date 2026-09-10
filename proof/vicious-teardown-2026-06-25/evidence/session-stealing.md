# Session stealing — default-open (code-confirmed, runtime by alloc agent)

- Ownership is TRUST-ON-FIRST-USE: session.py:1361-1375. claim_token check only runs
  if the CALLER voluntarily sets SIMEMU_SESSION_TOKEN. Unset by default -> `presented` is
  None -> no check -> any `simemu do <session_id> ...` proceeds.
- Session IDs enumerable by ANY local process: `simemu sessions --json` lists every active
  session id. session.py:156 id = "s-" + token_hex(3) (24 bits).
- Attack: co-tenant agent runs `simemu sessions --json` -> gets all ids ->
  `simemu do <victim-id> launch <pkg>` / `do <id> url ...` / `do <id> screenshot`.
  No authz. This is the CORE multi-agent isolation promise, default-broken.
FIX: bind session to owner (SIMEMU_AGENT and/or claim PID) and enforce by default;
make claim_token mandatory (auto-persist to a per-agent file the way the session id is handed back),
or check owner-agent/PID lineage on every `do`. Don't make isolation opt-in.
