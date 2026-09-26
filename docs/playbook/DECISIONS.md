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

## ADR-0012: Router details the spec leaves open
- Date: 2026-09-26 · Milestone/item: M3.7 · Status: accepted
- Context: §12 gives the score weights but not `cost_fit`, `latency_fit`, what "supports a capability" means, or how `routing.strategy` interacts with `routing.static`.
- Options: derive cost_fit from observed spend or from the manifest `cost_class`; treat any declared capability as supported or only priors > 0.
- Decision: `cost_fit` = manifest `cost_class` (free 1.0, low 0.75, medium 0.5, high 0.25), so it works with zero history. `latency_fit` = 0.5 without history, else `1 / (1 + p50_seconds / 300)`. An agent supports a capability only if its prior is > 0. Static pins apply under both `capability` and `static` strategies (a pin needs the pinned agent to pass health, capability, policy and budget checks, else the reason code `static_pin_ineligible:<id>` is recorded and scoring decides); `jev_assisted` tie-breaking is M6. A degraded pinned agent is ineligible while any ready agent exists. Expected cost only excludes an agent when both the remaining budget and that agent's expected cost are known. Ties break by agent id. `route()` is pure: stats, failed agents, the change author, budget and the policy predicate are passed in via `RoutingContext`.
- Consequences: Weights and fits live in `core/router/rules.py` constants and are covered by table tests. The `agent_stats` projection is not populated until M7, so early runs route on priors (observed = 0.5) which is the intended Beta(2,2) behavior.

## ADR-0013: Task edge `integrating --stop--> failed`; shared attempt helpers
- Date: 2026-09-26 · Milestone/item: M3.8/M3.9 · Status: accepted · Amends ADR-0006
- Context: §14.3 retries a merge conflict with `rebase_and_retry`, and ADR-0006 routes `integrating --merge_conflict--> ready`. Nothing lets a task fail when those retries run out: `ready` only leaves via assign, cancel, dependency failure or `no_eligible_agent`, and `integrating` had no failure edge.
- Options: reuse `no_eligible_agent` (wrong reason code); allow `ready --stop--> failed`; allow `integrating --stop--> failed`.
- Decision: add `(integrating, stop) -> failed`. The orchestrator, not the state machine, counts attempts: it decides before the conflicting merge whether a further attempt is allowed and sends `merge_conflict` (retry) or `stop` (exhausted, `FailureClass.MERGE_CONFLICT`). Also move `Emit`, `render_prompt`, `execute_agent`, `persist_artifacts`, `conflict_files` and the override helpers out of `single.py` into `core/orchestrator/attempt.py` so the multi-task executor shares them; `execute_agent` gains an `on_handle` hook so a scheduler can cancel a live attempt.
- Consequences: Behavior of the single-task path is unchanged (all M2 tests pass untouched).

## ADR-0014: `aix run` routes by default; `--agent` means single-task mode; cancellation transport
- Date: 2026-09-26 · Milestone/item: M3.8 · Status: accepted
- Context: M2 made `aix run --agent <id>` the only way to run and it ran the goal as one task. §23.1 lists `--agent` as optional and adds planning, routing and `--plan-only`. `aix cancel` must reach a run driven by another process.
- Options: make `--agent` force one agent for every planned task; keep it as the M2 single-task mode; remove it.
- Decision: `aix run "<goal>"` plans, routes and executes across agents (`core/orchestrator/executor.py`). `--agent <id>` keeps its M2 meaning (one task, that agent, no planning) so the M2 behavior and tests stay intact; with `--plan-only` it names the planner. Cancellation: Ctrl-C/SIGTERM set the run's cancel event; `aix cancel <run>` writes `.aix/runs/<run>/CANCEL`, which the live orchestrator polls every 250 ms, and waits for the run to become terminal. Runs nobody drives (`created`, `planned`, `waiting_approval`) are cancelled directly by appending events, and `--force` does the same for a crashed orchestrator. A cancelled run stops live agents through `adapter.cancel`, ends non-terminal tasks `cancelled` (blocked ones too) and finalizes as `cancelled` (exit 4). The test `test_run_without_agent_still_requires_one` asserted the M2 usage error and was rewritten to assert routing.
- Consequences: The single-task path is legacy and can be removed once `--agent` gains a "force for all tasks" meaning (post-MVP). Budget checks (§14.1) are not in the M3 scheduler; they arrive with the `budget` decision point in M5.10. Handoffs into dependent prompts (G2's last assertion) arrive in M6.

## ADR-0015: gemini and opencode adapter behavior discovered from the installed CLIs
- Date: 2026-09-26 · Milestone/item: M3.10, M3.11 · Status: accepted
- Context: Both CLIs are installed on the build machine (gemini-cli 0.55.1, opencode 1.18.26). CLAUDE.md §4 says to build against recordings when a CLI cannot be used live, and §3 requires an ADR for discovered CLI behavior and security trade-offs. No paid call was made: flags came from `--help`, event schemas from the installed code.
- Options: (a) prompt in argv; (b) prompt on stdin. Permissions: (a) pass the CLIs' auto-approve flags so agents can run shell commands; (b) stay in the safest mode that still allows edits.
- Decision: Both adapters send the task on **stdin**. Gemini needs `-p` to enter headless mode and appends its value to stdin, so `-p` carries a fixed non-sensitive instruction; opencode joins message args with stdin, so no positional message is passed. Gemini runs `--approval-mode auto_edit` (write) or `plan` (read-only) plus `--skip-trust`; never `yolo`. opencode runs the default `build` agent (write) or `--agent plan` (read-only) and never `--auto`/`--yolo`/`--dangerously-skip-permissions`, so permission prompts are auto-rejected. Neither CLI can express `write_scope`, `allowed_tools` or `network`, so both declare that gap in their docs and rely on the post-run scope check (§10.4); M8.3 must not treat them as scope-enforcing. opencode's `PWD` behavior is neutralized by not forwarding `PWD` and passing `--dir`. opencode has no result event, so completion is the last `step_finish` whose reason is not `tool-calls`. Gemini exposes no cost, opencode reports `cost` per step (0 is treated as unknown).
- Consequences: Gemini and opencode agents cannot run shell commands (tests, linters) themselves in write tasks; the control plane runs verification (§17), which is the intended split. Recordings for both are `synthetic: true` and gemini resume-by-id is unverified until M10.1 records real sessions. Both are in `BUILTIN_IDS` and the §10.5 matrix (5 kinds now).

## ADR-0016: Security checks: builtin secret scanner always on, network exceptions for audits
- Date: 2026-09-26 · Milestone/item: M4.5 · Status: accepted
- Context: §17.3 names gitleaks for `secrets`, semgrep/bandit for SAST and pip-audit/npm audit for deps. None of the four is installed on the build machine, and G7 must pass without them. Checks default to `network: deny` (§17.2), but audits and semgrep's `--config auto` fetch data from the network.
- Options: skip `secrets` when gitleaks is missing (unsafe: missing verification looks like success); depend on gitleaks; ship a builtin scanner.
- Decision: `secrets` always runs a builtin regex scanner over the **added lines of the patch** (AWS key ids, GitHub/Slack/Google tokens, private-key headers, quoted generic `key/secret/token/password = "<16+ chars>"`); when gitleaks is installed it also runs `gitleaks detect --no-git --redact` on the worktree and the findings are merged. Findings carry rule, path and line only, never the matched value. The check is always `required` and any finding is `failed`/`critical`; example keys such as `AKIAIOSFODNN7EXAMPLE` are deliberately not allowlisted. SAST and deps are `required` only when configured `on`; `high`/`critical` findings fail, lower ones warn; a missing tool is `skipped`. pip-audit has no severity field, so its findings count as `high`. Audits (`pip-audit`, `npm audit`) and semgrep with `--config auto` run with network allowed because they cannot work offline; semgrep uses a repo-local `.semgrep.yml`/`semgrep.yml` offline when present. bandit runs offline.
- Consequences: The builtin patterns are a floor, not a replacement for gitleaks; false negatives are possible for exotic formats. Tool availability is discovered per run, so results differ between machines and the report must record the command used (it does, via `Check.command`).

## ADR-0017: `pre_existing` is a `warning` check with a metric, not a new status
- Date: 2026-09-26 · Milestone/item: M4.7 · Status: accepted
- Context: §17.4 records a check that failed at baseline and still fails as `pre_existing`, but `Check.status` has five values and no such one; adding a value is a schema change after M1 that every consumer (report overall rule, gates, projections) would have to handle.
- Options: add a `pre_existing` status; add a boolean field; encode it in existing fields.
- Decision: a downgraded check has `status="warning"`, `severity="info"`, summary prefixed `pre_existing:` and metric `pre_existing=1`. `compute_overall` already treats a warning as non-blocking, which is exactly the intent. If the failure count (`tests_failed`, `lint_errors`, `type_errors`) is higher than at baseline the check stays `failed` and its summary says how many are new.
- Consequences: Reports and gates identify pre-existing failures by the metric. Baseline results are the `Baseline` model (JSON-serializable) and become an artifact in M7.

## ADR-0018: Verification gates acceptance; secrets are redacted centrally in the event store
- Date: 2026-09-26 · Milestone/item: M4.11 · Status: accepted
- Context: The M3 stand-in accepted any in-scope change. G7 requires that a planted key never appears in logs or artifacts, but the agent's own claim, streamed output and the stored patch can all contain it, and §20.5's full secrets policy is M8.
- Options: redact at each producer (claims, stream lines, patch files, events); redact once where events are persisted; leave it to M8.
- Decision: (1) The executor runs the verification engine for every write attempt (read-only tasks skip it), computes a baseline once per run on the untouched run branch, emits `check.finished` per check and `verification.completed`, and accepts only reports `passed` or `warning`. `failed` and `incomplete` reject with `FailureClass.VERIFICATION_FAILURE` (no retry until M5), and the first failing required check's summary is kept as `TaskSummary.detail`. (2) `aix.security.redact` holds the secret patterns (moved out of `verification`) and `redact_secrets`. `EventStore.append` redacts the serialized payload and rebuilds the payload from the redacted text so projections match a replay; the persisted patch, the stored normalized stream and `agent.output` text are redacted too. (3) `aix.core.orchestrator.executor.STUB_VERIFICATION` and the `m3:*` reason codes are gone.
- Consequences: A machine without build tools now gets `incomplete` and rejected attempts instead of silent acceptance; tests use `tests/verif_env.py` no-op commands. The raw agent stream file mirrored by adapters is not yet redacted (M8.x with the rest of §20.5). Redaction is regex based, so exotic secret formats can slip through.

## ADR-0019: Jev client: verified SDK surface and the neutral interface
- Date: 2026-09-26 · Milestone/item: M5.5 · Status: accepted
- Context: §18.4 asks for a verified `typesafe-sdk` signature before building the real client. The SDK is installed in the venv (optional extra `jev`).
- Verified by introspection: `AsyncTypeSafeClient(*, api_key, model, retry: RetryPolicy, timeout, headers, transport, http_client, base_url)` and `await client.system_one(state: JSONContent, questions: Mapping[str, Choice|Score|Noul], *, model, retry, timeout, extra_headers, extra_body, response_model) -> SystemOneResponse{model, usage{input_tokens, output_tokens}, answers}`. `Choice(criteria: Mapping[str, JSONContent|None], instructions)`, `Score(criteria: Sequence[JSONContent], instructions)` (level = list index), `Noul(criteria: {true, false} | None, instructions)`. Answers are `ChoiceAnswer{choice, confidence, probabilities}`, `ScoreAnswer{score: float, confidence, legend, probabilities}`, `NoulAnswer{noul: float}` (a yes probability). The SDK has both sync and async clients, so the async one is used directly (no thread hop). Retries are set to 1 and the call carries an explicit timeout.
- Decision: aiX code depends only on `aix.decision.jev` (`JevClient` protocol, `JevQuestion`/`JevAnswer`/`JevResponse`, `JevError`, `FakeJevClient`). `providers/jev.py` is the only importer of the SDK (import-linter contract) and maps our types to and from the SDK's; every SDK failure is re-raised as `JevError`, which the Decision Service turns into `jev:unavailable` and a rules fallback. Without `TYPESAFE_API_KEY` construction fails with a clear `JevError`. Live tests are `@pytest.mark.live` (`tests/live/test_jev_live.py`); nothing in the default suite calls the network.
- Consequences: The SDK's confidence semantics (calibration) are unverified until the live eval (M5.7). Only `noul` carries no confidence field, so thresholds use the probability itself.

## ADR-0020: Decision-driven retries in the executor
- Date: 2026-09-26 · Milestone/item: M5.9-M5.10 · Status: accepted · Amends ADR-0014, ADR-0018
- Context: With verification in place (M4) the executor still rejected any failed attempt. §18-§19 require a recorded decision after every attempt, retry mutations, an escalation ladder, attempt and budget limits, and human approvals.
- Decisions:
  1. **Flow.** After an attempt: process failure -> `failure_triage`; verified attempt -> `task_completion` (gates first). The outcome maps to a `NextAction` (`core/orchestrator/task_policy.py`): `accept` merges; `retry`/`switch_agent` take the next mutation of the failure class's §19.2 sequence (a `switch_agent` outcome forces a switch); `escalate` climbs the ladder; `ask_human` records a pending `Approval` and parks the task (`waiting_approval`, run exit 3); `reject`/`stop` fail it. Decisions are recorded as `decision.requested` + `decision.completed`.
  2. **Same agent means same agent.** The router's failed-agent penalty would otherwise switch agents on a `same_agent_*` mutation, so those (and `wait_and_retry`) pin the previous agent. `switch_agent` excludes it, falling back to it if nobody else is eligible.
  3. **Attempts.** `consumed` counts toward `max_attempts`; non-consuming steps (rate limit / network waits, auth switch) refund it. Every retry prompt carries a unique control-plane note (never agent prose) and an identical (agent, prompt hash, base commit) retry raises `IdenticalRetry`.
  4. **Ladder.** `execution.escalation_ladder` (default stronger_model, different_agent, multi_agent, human). Inapplicable steps are skipped. An empty ladder means "no human fallback" and the task is rejected, which tests use for hard-fail semantics.
  5. **Budget.** `BudgetTracker` is checked before every attempt and after every attempt (cost and wall time strictly greater than the limit, attempts before starting). An overrun emits `budget.exceeded`, asks the `budget` decision point (gates allow only stop/ask_human), stops scheduling and ends the run failed with `BUDGET_EXCEEDED` (exit 6). A budget `ask_human` records the approval but the run still stops; resuming after a budget override is not implemented.
  6. **`split_task`.** A timeout replaces the task with three chained subtasks (deterministic split); dependents are re-wired **in memory only** because no event records a dependency change, so `aix status` (projections) can still show the old dependency. The original becomes `cancelled` with reason `split_into:<ids>` and is excluded from completion checks.
  7. Read-only tasks skip verification; their decision uses an empty `passed` report.
- Consequences: A run that cannot fix a task now ends `waiting_approval` (default ladder) instead of failing; resuming after `aix approve` is M5.11. The rules table decides unless `decision.provider: jev` is configured and reachable (missing SDK/key silently uses rules; the record names the provider that decided).

## ADR-0021: Approval resolution and resume
- Date: 2026-09-26 · Milestone/item: M5.11 · Status: accepted
- Context: §20.4 requires TTY confirmation or a token, an agent-context guard, and that the run resume after a grant; `aix run` had no way to continue.
- Decision: `security/approvals.py` holds the guards: a token file in `$XDG_CONFIG_HOME/aix/approval_token` (mode 0600, created on first use, compared in constant time), typed short-id TTY confirmation, and refusal when `AIX_AGENT_CONTEXT` is set (the env builder now always sets it to `1` for agents and it cannot be overridden). `aix approve|deny <approval_id|task_id>` authenticate first; `aix approvals` lists pending ones and `--show-token` prints the token (refused in agent context). Granting moves a waiting task to `ready` and, unless `--no-resume`, calls `resume_run`, which rebuilds the graph from projections, re-seeds attempt numbering, prior attempt count and cost (budget still applies), authors and `split_task` supersessions (from the `split_into:` reason code) and continues; per-task retry counters start fresh, since a human decision grants another round. Denying fails the task with `HUMAN_REJECTION`, cancels every other non-terminal task and fails the run (exit 1). The scheduler can now start `ready` tasks directly.
- Consequences: The token is readable by any same-user process (the spec's documented residual risk in `local` mode; not mounted in `container` mode, M8). `aix run --resume <run>` (crash recovery) is M7; a budget-override approval is recorded but not resumable. Approvals resumed from a different process rebuild fakes/agents from config, so scripted test agents restart their scripts.

## ADR-0022: plan_review and tool_risk in the control plane
- Date: 2026-09-26 · Milestone/item: M5.12 · Status: accepted
- Context: §18.2 puts a `plan_review` decision after planning for high-risk intents and a `tool_risk` decision before control-plane executed gated commands. The control plane executes only verification commands (checks and the baseline run), not agent tool calls.
- Decision: **plan_review**: `execute_run` asks the point after planning when `intent.risk == "high"`; the rules provider returns `ask_human` (so `aix run` exits 3 with nothing executed and a pending `plan_review` approval on the run), `accept` proceeds, `reject` fails the run (`POLICY_FAILURE`). It is deliberately not gate-forced, so a configured provider may still accept. Granting resumes through `resume_run` from the `planned` graph (all tasks `created`). **tool_risk**: `core/toolrisk.py` classifies argv into control-plane labels (`read_only`, `local_write`, `local_delete`, `git_push`, `deploy`, `db_migration_apply`, `external_network`, `unknown`; `sh -c` strings are split on `&&`/`;`). Ordinary build/test/lint commands (`read_only`, `local_write`) pass silently, without a record. Anything else is decided (gates first: a class named in `security.approval_required_for` forces `ask_human`; `git_push` maps to the config action `push`) and recorded. The gate wraps both the attempt's verification and the baseline run; a refused command becomes an `error` check (`command refused: tool_risk ...`) and is not executed.
- Consequences: An `ask_human` on a control-plane command is refused rather than pausing mid-verification; approving it means adding the command to config or removing it from `approval_required_for`. Commands run by agents themselves (their own tool calls) are outside this point until the container/policy work in M8. The classifier only sees the executable and a few sub-command words, so a wrapper script can hide intent; the allowlist (`security.shell_allow`) remains the first line of defense.

## ADR-0023: Context pack budgeting and token estimate
- Date: 2026-09-26 · Milestone/item: M6.3 · Status: accepted
- Context: §15.3 wants a per-agent context budget (default 24k tokens) with priority truncation. Vendors tokenize differently and the core must stay provider-agnostic; a tokenizer dependency per vendor is not justified.
- Decision: Tokens are estimated as `ceil(chars / 4)` (`core/context/pack.py::estimate_tokens`), deliberately conservative for English and code. New config key `execution.context_budget_tokens` (default 24000, min 500; schema change, hence this ADR). The pack keeps sections in priority order (goal and constraints, dependency handoffs, previous-attempt failures, project facts, file hints); the first section that does not fit is cut at a line boundary with a `[truncated]` marker and every lower-priority section is dropped and named in `dropped`. The first section (goal and constraints) is never dropped, only cut if it alone exceeds the budget. All text passes through `redact_secrets`.
- Consequences: The estimate can be off by tens of percent for non-English text; the budget is a guard rail, not an exact limit. Swapping in a real tokenizer changes one function.

## ADR-0024: Artifact store, manifest and bundles
- Date: 2026-09-26 · Milestone/item: M7.1-M7.4 · Status: accepted
- Context: §21 asks for a content-addressed store, standard artifacts, a hashed manifest and a verifiable bundle. The `Artifact` model has no name field, and artifact events had no writer.
- Decision: Blobs live at `.aix/artifacts/objects/<sha[:2]>/<sha>` (atomic write, a re-put repairs a corrupt blob). Text media types are secret-redacted before hashing. Names such as `patch/<task>.diff` exist only in `manifest.json` (`name`, `artifact_id`, `type`, `sha256`, `size`), written last and listing everything before it, including `report.md`/`report.html`. It is written once a run is COMPLETED, FAILED or CANCELLED, never while `waiting_approval`, so a resumed run produces a second set. `aix artifact export --bundle` (also automatic when `artifacts.bundle` is true) writes a deterministic zip (sorted entries, fixed timestamps) with `manifest.json` and `manifest.sha256`; `verify` re-hashes everything and reports missing, extra, modified or unsafe entries. New event ordering: `artifact.created` events follow the terminal run event.
- Consequences: The bundle protects against accidental corruption and partial edits, not a forger who rewrites both manifest files; comparing `manifest.sha256` with the manifest artifact hash in the event store closes that, and signing is deferred. The legacy single-agent `--agent` path writes no artifacts.

## ADR-0025: Reported-or-estimated cost and the price table
- Date: 2026-09-26 · Milestone/item: M7.5 · Status: accepted
- Context: Only some CLIs report `cost_usd`; §22 wants estimates from a config price table, marked `estimated`, and budget enforcement on the reported-or-estimated figure.
- Decision: New config `pricing.models` (model id or id prefix -> USD per million input/output tokens; schema change). It is **empty by default**: aix ships no guessed prices. `core/cost.py::estimate_usage` fills a missing cost from token counts and a price for the attempt's model (longest prefix wins) and sets `estimated`; a reported cost is never replaced and unknown stays unknown (never 0). It runs right after the agent finishes, so the budget tracker, results, and reports all see the same number. Reports split `reported + estimated`.
- Consequences: Without a price table cost-free CLIs (codex, gemini) are simply uncounted, so the cost budget cannot stop them; attempt and wall-time budgets still apply.

## ADR-0026: Crash recovery and `aix run --resume`
- Date: 2026-09-26 · Milestone/item: M7.8 · Status: accepted
- Context: §8.2 requires that after the orchestrator dies, attempts still `running` become `failed` with `INTERRUPTED` and normal retry logic applies; accepted-but-unintegrated tasks resume at integration.
- Decision: `execute_graph` writes `.aix/runs/<run>/orchestrator.pid` and removes it on exit. `aix run --resume <run>` (`resume_interrupted_run`) refuses finished runs, runs waiting for approval (use `aix approve`), runs interrupted while planning (start a new run) and runs whose pid file names a live process. Otherwise `core/orchestrator/recovery.py` finishes every `created`/`running` attempt as `failed`/`INTERRUPTED`, removes its worktree (branch kept), and moves stranded tasks back to `ready` via existing legal transitions (`ACCEPTED`/`INTEGRATING` use `MERGE_CONFLICT` as the edge back to `ready`, reason `failure:interrupted`). A task whose attempt already has a `workspace.merged` event is completed instead. Re-executed tasks get `same_agent_new_context` and a control-plane note in their prompt. A `planned` run that never started just executes; a `finalizing` run skips the repeated `all_tasks_terminal` transition. Prior attempts and cost count against the budget, as for approvals.
- Consequences: Accepted-but-unmerged work is redone rather than re-integrated (its worktree is discarded; safe, but costs a repeat). A recycled pid is treated as alive; deleting the pid file forces a resume. Per-task retry counters restart, as after an approval; the run-level attempt budget still bounds a crash loop.

## ADR-0027: Policy engine, native-enforcement flags and the policy hash
- Date: 2026-09-26 · Milestone/item: M8.1, M8.3 · Status: accepted
- Context: §20.2 wants a versioned policy engine and agent ineligibility for high-risk tasks when a CLI cannot enforce the needed restriction. Permissions were built inline in the executor, the decision hash used a constant `policy-v1`, and the router's `policy_allows` hook was unused.
- Decision: `security/policy.py::Policy` (built from `SecurityConfig`) answers `can_run_agent`, `agent_permissions`, `can_exec`, `requires_approval` and `can_write`. Its `hash` is SHA-256 of `policy-v2` plus the effective security config; it replaces `policy-v1` as the DecisionService policy version and is recorded as `policy_hash` in artifact provenance. New manifest field `supports.enforces` (subset of `read_only`, `write_scope`, `network_deny`; schema change) states what each CLI enforces natively: claude read_only + write_scope, codex read_only + network_deny, gemini and opencode read_only, fake all. In `local` mode a `high`-risk task is routed only to agents enforcing every restriction it depends on (read-only tasks need `read_only`; narrowed file scopes need `write_scope`; `["**"]` needs nothing); in `container` mode the rule is skipped because the container confines the filesystem. `aix dev eval-decisions --replay` takes `--policy-hash` (default: the default config's hash).
- Consequences: Old decision records (made under `policy-v1`) no longer replay with matching hashes; replay them with the hash they were made under. With the default adapters a high-risk *scoped write* task can only go to claude or fake; if none is enabled the run fails with NO_ELIGIBLE_AGENT rather than running unconfined. Per-adapter permission-to-flag translation is snapshot-tested (`tests/fixtures/permissions/`).

## ADR-0028: Known-secret redaction registry
- Date: 2026-09-26 · Milestone/item: M8.2 · Status: accepted
- Context: §20.5 wants a redactor with the *values* of secrets from the environment, not only key-shaped regexes. Redaction happens in the event store, artifact writer, prompt capture, patch capture, stream capture and check output, none of which can all be handed a redactor object.
- Decision: `security/redact.py` keeps a process-wide tuple of known secret values, set once by the CLI callback (`configure_known_secrets`) from environment variables whose *names* look like credentials (api key, token, secret, password, credential, private key, auth; values of 8+ characters) plus the approval token if its file exists. `redact_secrets` removes them (longest first) in addition to the regexes. This is a deliberate exception to "no global singletons"; it is set once and only ever shrinks what can be written. Raw agent streams are scrubbed after each attempt, and check stdout/stderr are redacted before they are written. `tests/security/test_secret_leak.py` runs a fake run whose agent echoes a shapeless key everywhere and scans every file under `.aix/` (database, streams, prompts, artifacts, unzipped bundle) for it; with the registry disabled the same test fails.
- Consequences: A secret held in a variable with an innocuous name, or shorter than 8 characters, is not caught; regexes still catch common shapes. Redaction is textual, so a secret transformed by the agent (base64, split) is not caught. Files the agent itself writes into the worktree are the user's repo content and are not rewritten (patch artifacts are redacted).
