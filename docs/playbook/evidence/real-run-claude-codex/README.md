# Real end-to-end run (claude + codex), 2026-09-27

`aix run "Add a function greet(name) in app/greeting.py ... with tests in tests/test_greeting.py"` on a
scratch copy of the fixture repo, real agents, real toolchain checks, budget capped at $1.50.

- Planner: claude (agent planner). implement + test: codex. Independent review: claude.
- 3/3 tasks completed, 0 retries, 14 tests passed, 3 decisions (rules), cost $0.16, 1m57s.
- The merged branch `aix/run/<id>` holds exactly `app/greeting.py` and `tests/test_greeting.py`;
  running pytest on it independently gave 9 passed.
- The full verifiable bundle (31 files, `aix artifact verify` OK) is **not** committed: its raw agent
  streams contain the machine's local Claude plugin inventory and paths. Only the human-readable
  parts are kept here (paths to the scratch repo replaced by `<scratch-repo>`).
- Earlier attempts of this same run exposed three product bugs, fixed in ADR-0036.
