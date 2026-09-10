# Dead advertised commands + test-suite state (runtime-proven 2026-06-25)

## Dead advertised commands (P1, REAL)
`simemu --help` advertises ~65 subcommands, each with its own `--help` page, but only
~16 are live (the _V2_COMMANDS allowlist in cli.py:2905). The rest hard-reject at dispatch:
  $ simemu boot x   -> "Error: 'boot' is not a recognized command." exit=1
  same for: screenshot apps clipboard erase present focus stabilize ready (+ ~40 more)
Real per-device ops are only reachable via `simemu do <session> <op>`. The top-level
slug commands are registered in argparse (so they pollute --help and have help pages)
then refuse to run. A commercial CLI whose --help lists 49 commands that don't work is a
trust/UX defect.
FIX: stop registering legacy subparsers (or hide them) so --help only shows live commands;
OR fully wire them as thin `do`-shims. Don't advertise dead commands.

Exit codes ARE correct: legacy-reject=1, unknown-command=2 (argparse), success=0.

## Test suite not green out-of-box (P2, REAL)
`python3 -m pytest -q` on this target (Python 3.14.4, Xcode 26.5): 5 failed, 643 passed.
- test_android.py crash-log .decode failures: mock returns str, real subprocess returns
  bytes -> mock/contract drift (harness bug, not product).
- test_ios.py launch test: mock of ios._simctl missed a direct subprocess.run fallback ->
  hit REAL `xcrun simctl launch SIM-001` (exit 148). A unit test executing real simctl is
  dangerous + flaky; also shows product bypasses its own _simctl wrapper on one path.
- test_device.py idevicescreenshot: depends on tunneld/idevicescreenshot in env -> not mocked.
A shipped commercial tool should have a green suite on its supported toolchain.

## Top-level error handling (code, for cli agent to expand)
main() (cli.py:2948-2961) catches only RuntimeError -> clean message. Any other exception
(SessionError variants, CalledProcessError, OSError, KeyError) prints a raw Python traceback.
