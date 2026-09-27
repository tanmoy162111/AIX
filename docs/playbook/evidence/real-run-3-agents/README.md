# Real end-to-end run: three different agents, 2026-09-27

`aix run "Add search and pagination to the users store ... route ... tests ... docs/USERS.md"` on a
scratch copy of the fixture repo, real agents, real checks, budget capped at $3.

- claude planned, inspected and documented; codex implemented and tested; opencode reviewed.
- 7/7 tasks, 0 retries, 58 checked tests passed, $0.37, 4m49s.
- Merged branch holds 5 files (+225/-2); running pytest on it independently: 25 passed.
- `bundle.zip` is the full verifiable bundle (`aix artifact verify` → OK, 66 files). Its agent streams
  were sanitized when written (ADR-0038); a scan found no home paths or keys. It still contains what
  the agents said and did.
- An earlier attempt of the same goal stopped for approval after a reviewer's
  `find . -path ./.aix -prune` was mis-flagged as tampering; fixed in ADR-0040. That attempt also showed a
  real auth failure from gemini being handled by switching to claude.
