# DECISIONS — Architecture Decision Records

Append-only. Never edit an accepted ADR; supersede it with a new one (`Supersedes: ADR-NNNN`).
Template:

```markdown
## ADR-NNNN: <title>
- Date: YYYY-MM-DD · Milestone/item: M?.? · Status: accepted | superseded by ADR-XXXX
- Context: <what forced a decision; cite PLAYBOOK §>
- Options: <A / B / C with one-line trade-offs>
- Decision: <chosen option>
- Consequences: <what gets easier/harder; follow-ups>
```

---

## ADR-0001: Seed decisions carried over from playbook v2
- Date: 2026-09-26 · Milestone/item: pre-M0 · Status: accepted
- Context: Playbook v1 was architectural and left implementation choices open. v2 fixes them so the
  build can proceed without human input.
- Decisions (each may be superseded by a later ADR with evidence):
  1. Python 3.12 + uv + typer/rich + pydantic v2 + anyio + SQLite (§4). Rationale: fastest path to a
     typed, testable CLI; all agent CLIs are subprocesses so language performance is not a bottleneck.
  2. Event-sourced state in SQLite with rebuildable projections (§8). Rationale: traceability and
     crash resume are core requirements; SQLite is zero-ops.
  3. Git worktree per write attempt, results on `aix/run/<id>`, user branch never touched without
     `--apply` (§14.3). Rationale: isolation, parallel writes, reversibility.
  4. Default decision provider = `rules`; Jev is opt-in until the §18.7 eval shows parity or better.
     Rationale: Jev is a new external dependency; gates must work offline; decisions must be reproducible.
  5. Jev receives only control-plane facts (numbers, enums, short labels), never agent prose or repo
     content (§18.6). Rationale: Jev does not treat state as hostile; this closes the injection path.
  6. Agent completion claims are stored but never used as evidence (§10.4).
  7. Real CLI adapters are built against recorded streams + flag discovery at runtime, because CLI
     flags change between versions (§11, Appendix C).
  8. Web dashboard, swarm, remote workers, long-term memory are out of scope for this build (§32).
- Consequences: The whole system is buildable and testable offline with fakes. Live behavior of
  each vendor CLI and Jev is validated in M10 and documented, not assumed.

## ADR-0002: Dependency set and build backend
- Date: 2026-09-26 · Milestone/item: M0.3 · Status: accepted
- Context: CLAUDE.md §3 requires an ADR for every added dependency; PLAYBOOK §4 names the stack.
- Options: hatchling / setuptools / uv_build as build backend; `ulid-py` / `python-ulid` for prefixed ULIDs; bandit+pip-audit in `security` extra vs. leaving them as external tools.
- Decision: hatchling (src layout, no config). Runtime: typer, rich, pydantic, pydantic-settings, anyio, aiosqlite, pyyaml, jinja2, structlog, python-ulid. Extras: `jev` (typesafe-sdk, verified resolvable on PyPI 2026-09-26), `api` (fastapi, uvicorn), `security` (bandit, pip-audit; semgrep and gitleaks are external binaries, not Python deps), `dev` (pytest, pytest-asyncio, pytest-cov, hypothesis, syrupy, ruff, pyright, import-linter, types-pyyaml). Interpreter pinned to 3.12 via `.python-version` (system default is 3.14; 3.12 is the playbook floor and what uv provides).
- Consequences: `uv sync --all-extras` installs everything for development. Version floors are loose; `uv.lock` is the source of reproducibility. Adding any further dependency needs a new ADR.

## ADR-0003: Interpretation of the §5.3 layering contract
- Date: 2026-09-26 · Milestone/item: M0.5 · Status: accepted
- Context: §5.3 lists which packages the core-tier (core, verification, decision, artifacts, security, observability, skills, tools) "may import". Read literally, peers could not import each other, yet the orchestrator must call verification and decision (§14.2).
- Options: (a) forbid all peer imports; (b) enforce only the explicit "Forbidden" list plus the unambiguous isolation rules; (c) impose an internal ordering among core-tier packages.
- Decision: (b). Enforced contracts: domain imports nothing else in aix; core-tier (and store/config/domain) never import `agents.adapters` or `agents.subprocess`; adapters never import core-tier, store, registry, api or cli; nothing imports `aix.cli`; only cli may import `aix.api`; `typesafe_sdk` only from `decision/providers/jev.py`. `agents.registry` is deliberately excluded from the core-never-imports-adapters sources and will discover adapters via entry points/`importlib` strings, not static imports (M2.3/M9.3).
- Consequences: Peer imports inside the core tier are allowed; revisit with a stricter ordering contract if cycles appear. Empty stub modules (`agents/protocol.py`, `registry.py`, `subprocess.py`, `decision/providers/jev.py`) exist so contracts can name them.

## ADR-0004: Use anyio's pytest plugin; drop pytest-asyncio
- Date: 2026-09-26 · Milestone/item: M0.6 · Status: accepted · Amends ADR-0002
- Context: PLAYBOOK §4 says "pytest-asyncio (anyio)"; the codebase is anyio-only (CLAUDE.md §7). Two async plugins would both be loaded, and pytest-asyncio emitted a loop-scope deprecation warning.
- Options: keep both; keep pytest-asyncio only; anyio's built-in plugin only.
- Decision: anyio plugin only (`@pytest.mark.anyio`, backend pinned to asyncio in `tests/aix_pytest_plugin.py`). `pytest-asyncio` removed from the `dev` extra.
- Consequences: async tests must carry `@pytest.mark.anyio`. Trio is not tested.
