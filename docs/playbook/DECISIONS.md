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

## ADR-0005: Domain model shape where §6 leaves fields open
- Date: 2026-09-26 · Milestone/item: M1.2 · Status: accepted
- Context: §6 shows "only key fields" and says extra fields need an ADR. It also writes outcomes as `choose:<x>` and uses bare `dict` for decision payloads, while CLAUDE.md §7 forbids bare dicts across modules.
- Options: encode `choose:<x>` as a parametrised string vs. an enum plus a separate field; `dict[str, Any]` vs. `dict[str, JsonValue]`.
- Decision: (1) `DecisionOutcome` is a plain enum with a `choose` member and `DecisionRecord.choice: str | None` holds the argument; a validator requires `choice` exactly when outcome is `choose`. (2) Decision/approval payloads are `dict[str, JsonValue]`. (3) Models are frozen with `extra="forbid"`, timestamps are timezone-aware. (4) Supporting types defined here because §6 references but does not define them: `Budget`, `VerificationSpec{required,optional}`, `AgentSupports`, `DiffSummary{files_changed,lines_added,lines_removed,paths,patch_sha256}`, `ToolCallRecord`, `Usage{...,estimated}`, `ArtifactRef`, `GateResult`, `Producer`, `Provenance`, `ArtifactType`, `Capability`, `VerificationFailureKind`, `AttemptStatus`. (5) Extra validators: `Task.max_attempts` in 1..6 (§19.3), `VerificationReport.overall` must equal `compute_overall(checks)`, `Run.finished_at` only for terminal status, decided `Approval`s need actor/channel/decided_at.
- Consequences: Schema changes after M1 need a new ADR (CLAUDE.md §3), so this is the last cheap moment to adjust shapes. Consumers read the `choose` argument from `choice`.

## ADR-0006: State machine edges added beyond the §7 diagram
- Date: 2026-09-26 · Milestone/item: M1.4 · Status: accepted
- Context: §7 draws the happy path and decision branches but §14.2, §12 and §19 need a few more edges (failed attempts, routing failure, merge conflicts, plan review pause).
- Options: leave them out and handle by ad-hoc status writes; or add explicit table edges.
- Decision: keep table-driven; add task edges `running|assigned --attempt_failed--> deciding` (a crashed/interrupted attempt skips verification and goes straight to failure triage), `ready --no_eligible_agent--> failed` (§12), `integrating --merge_conflict--> ready` (§14.3, mutation `rebase_and_retry`). Run edges: `planned --await_approval--> waiting_approval` (plan_review, G4), `waiting_approval --approval_denied|fail--> failed`, `finalizing --fail--> failed` (run_completion rejected). `blocked` is not terminal (spec lists three terminals) but only leaves via `cancel`. `DEPENDENCY_FAILED` is accepted from any live non-blocked state. `transition_*` raise `IllegalTransition` instead of returning it (Pythonic form of the spec's `Task | IllegalTransition`); run transitions take `at` so the domain never reads the clock.
- Consequences: Later milestones must use these events; new edges need an ADR. Tests assert reachability, absorption and totality over the whole (status × event) matrix.

## ADR-0007: `aix init` writes a commented config and extra .gitignore entries
- Date: 2026-09-26 · Milestone/item: M1.9 · Status: accepted
- Context: §5.2 says `aix init` adds `.aix/worktrees/` and `.aix/runs/` to `.gitignore` and creates a default config. The runtime dir also holds `aix.db` (+ `-wal`/`-shm`) and `artifacts/`, which must not be committed by accident. Writing every default as active YAML would make every key look "project"-sourced in `config show --resolved` and would pin today's defaults against future upgrades.
- Options: write full defaults; write an empty file; write the defaults commented out.
- Decision: write the defaults commented out (valid empty YAML, so resolution is unchanged and sources stay `default`). Ignore `.aix/worktrees/`, `.aix/runs/`, `.aix/artifacts/`, `.aix/aix.db`, `.aix/aix.db-wal`, `.aix/aix.db-shm`; `.aix/config.yaml` stays committable. `init` also creates/migrates the database so `aix dev rebuild-projections` works immediately. `--force` rewrites only the config, never the database.
- Consequences: A superset of the spec's two entries; teams can share `.aix/config.yaml`. Toolchain detection is a minimal marker scan (`verification/detect.py`) to be extended in M3.1/M4.1.

## ADR-0008: `AgentSpec.health_reason` and manifest extensions
- Date: 2026-09-26 · Milestone/item: M2.3 · Status: accepted · Amends ADR-0005
- Context: §11 requires "health = `degraded` with a reason", and §20.5 puts per-adapter env allowlists in the manifest; §6's `AgentSpec` has no place for the reason.
- Options: keep the reason outside the model (side channel); add an optional field.
- Decision: add `AgentSpec.health_reason: str | None = None` (schema change, exported and drift-tested). Manifest gains `required_flags`, `env_allowlist`, `models`, `default_model` and `probe.help_args`. `probe.auth_check` (a paid smoke call) is never run by default probing.
- Consequences: `aix agent list` can explain non-ready health. The field is optional, so existing JSON stays valid.

## ADR-0009: `RepoFacts` domain model for the repo-facts inspector
- Date: 2026-09-26 · Milestone/item: M3.1 · Status: accepted
- Context: §13.1/§15.1 need repo facts (languages, package managers, test commands, size) as input to the intent engine, planner and context fabric, but §6 defines no model. Core must consume it under pyright strict without importing `verification`.
- Options: return a `verification`-owned model; return a bare dict; add a domain model.
- Decision: add `aix.domain.tasks.RepoFacts{languages, package_managers, test_commands, convention_files, file_count, total_bytes, is_git_repo}` (a new exported schema `schemas/repo_facts.json`). `verification.detect.inspect_repo(root)` builds it from `detect_toolchain` plus lockfiles and a size walk that skips `.git`, `.aix`, `node_modules`, venvs and symlinks. It reads marker files and metadata only, never source contents.
- Consequences: New schema after M1 (additive). Command resolution for build/lint/typecheck stays in M4.1; `test_commands` here is a hint for prompts, not the verification command source of truth.

## ADR-0010: AgentPlanner wire format is a key-based `PlanDraft`, not a `TaskGraph`
- Date: 2026-09-26 · Milestone/item: M3.5 · Status: accepted
- Context: §13.2 says the planner prompt embeds "the TaskGraph JSON Schema". A `TaskGraph` needs ULID ids, a `run_id`, a `graph id`, task `status` and `max_attempts`, none of which an agent can or should invent.
- Options: ask the agent for ULIDs and validate them; embed the TaskGraph schema and rewrite ids afterwards; define a smaller draft schema with local keys.
- Decision: the agent returns `PlanDraft{tasks:[{key,title,goal,type,skill?,required_capabilities,depends_on(keys),file_scope,verification,risk?}]}` (`core/planner/draft.py`, embedded in the prompt as JSON Schema). `parse_plan` validates it (unique keys, known dependencies, no self-dependency, acyclic, known skill), assigns fresh ULIDs, rewrites dependencies, inherits `risk` from the intent and builds the real `TaskGraph`, which re-checks the domain invariants. Capabilities are free strings on the wire; unknown names are dropped with a warning (§13.3). Extra fields are rejected so the repair loop can name them.
- Consequences: The draft is a planner-internal type and is not in the exported schema set. `AgentPlanner` takes an injected `PlannerRunner` (prompt to final message); `make_adapter_runner` runs an adapter read-only in a throwaway worktree, rejects any modified file, and deletes the worktree and its temporary branch. Emitting planner-run events and the `planner.provider` selection are wired in M3.6.

## ADR-0011: `run.planned` records the planner and warnings; `auto` never picks the built-in `fake`
- Date: 2026-09-26 · Milestone/item: M3.6 · Status: accepted
- Context: §13.3 says unknown capabilities are dropped with a warning and the plan is persisted as `run.planned`; nothing records which planner produced it or what post-processing changed. Separately, §13.2 makes an agent planner the default when any agent has `design >= 0.6`, but the built-in `fake` agent is always enabled and declares `design: 0.6`, so a real run would pick it.
- Options: add new event types; add optional fields to `run.planned`; leave the information out. For `fake`: exclude by id, or drop its design prior.
- Decision: `RunPlannedPayload` gains `planner: str = "template"` (`template` or `agent:<id>`) and `warnings: list[str] = []` (fallback reasons, dropped capabilities, merged tasks, added edges). Additive with defaults, so events written before this change still replay. `planner.provider = auto` never selects the id `fake` (`fake-*` and an explicit `agent:fake` are allowed). `--plan-only` leaves the run in status `planned`; the run branch is created only when an agent planner needs a worktree.
- Consequences: Schema `event_run_planned.json` re-exported. A planned-but-never-executed run stays `planned` (non-terminal) until a later milestone adds resume/approve. Tests that exercise planner selection must use a hermetic PATH (`tests/cli_env.hermetic`) because `auto` will use any real, enabled agent CLI it finds.
