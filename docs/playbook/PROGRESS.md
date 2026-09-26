# PROGRESS — aix build checklist

Legend: `[ ]` todo · `[x]` done · `[~]` partial (see Blocked) · IDs are stable: cite them in commits (`[M3.4, §12]`).
Rule: work the **first unchecked item of the current milestone**. A milestone is done only when its
PLAYBOOK §30 Exit Gate passes and `git tag m<N>-done` exists.

**Current milestone:** M5
**Environment notes (M0.1, probed 2026-09-26):** python=3.14.4 system, 3.12.13 via uv (project pins 3.12), uv=0.11.17, git=2.53.0, docker=29.5.2, podman=missing, claude=2.1.283, codex=0.147.0, gemini=0.55.1, opencode=1.18.26, ollama=0.24.0, TYPESAFE_API_KEY present=no, semgrep=missing, gitleaks=missing, pip-audit=missing (auth state of agent CLIs not probed; live tests gated by AIX_LIVE)

---

## M0 — Scaffold (§4, §5, §26)
- [x] M0.1 Probe environment (`which`/`--version` for every tool above); record results in "Environment notes"; ADR-0002 if anything required is missing.
- [x] M0.2 `git init`, `.gitignore` (incl. `.env*`, `.aix/`, `__pycache__`, `.venv`), initial commit of playbook docs.
- [x] M0.3 `pyproject.toml` (src layout, extras `jev`,`api`,`security`,`dev`), `uv lock`, console script `aix = aix.cli.main:app`.
- [x] M0.4 Package tree per §5.1 with `__init__.py` files and module docstrings.
- [x] M0.5 Tooling configs: ruff, pyright (strict on core/domain/decision), `.importlinter` contracts from §5.3.
- [x] M0.6 pytest config: anyio, markers (`live`, `slow`), socket blocker plugin, `AIX_LIVE` gate, coverage config.
- [x] M0.7 Makefile targets: `fmt`, `fmt-check`, `lint`, `typecheck`, `layers`, `test`, `golden`, `test-live`, `check`.
- [x] M0.8 Minimal typer app with `--version` and `--help`; smoke test.
- [x] M0.9 Run M0 Exit Gate; tag `m0-done`.

## M1 — Domain, store, config (§6–§9)
- [x] M1.1 ID types (prefixed ULIDs), enums (`TaskType`, `Capability`, `CheckKind`, `FailureClass`, `RetryMutation`, `DecisionPoint`, `DecisionOutcome`, statuses).
- [x] M1.2 Domain models from §6 with validators (TaskGraph invariants: acyclic, unique ids, deps exist).
- [x] M1.3 Typed errors in `aix.domain.errors`, each mapped to a `FailureClass`.
- [x] M1.4 Run + Task state machines (§7), table-driven; hypothesis state-machine tests; terminal absorption; blocked propagation.
- [x] M1.5 `aix dev export-schemas` → `schemas/*.json`; drift test.
- [x] M1.6 SQLite store: migrations, WAL, append-only trigger on `events`, event payload schemas per type (§8.3).
- [x] M1.7 Projections updated transactionally; `aix dev rebuild-projections`; replay-equality test.
- [x] M1.8 Layered config loader (§9) with source tracking; `aix config show --resolved`.
- [x] M1.9 `aix init` (creates `.aix/`, default config, gitignore entries, detects toolchain summary).
- [x] M1.10 Run M1 Exit Gate; tag `m1-done`.

## M2 — Adapters, registry, single-task run (§10, §11, §14.3 partial, §23 partial)
- [x] M2.1 `AgentAdapter` protocol, `AgentRequest/Event/Outcome/Handle`, `AgentPermissions`.
- [x] M2.2 Shared subprocess runner: process groups, streaming, timeout→TERM→KILL, stderr ring buffer; tests with tiny scripts.
- [x] M2.3 Adapter manifest schema + registry (discover via builtin list now; entry points in M9), health, enable/disable persisted in config.
- [x] M2.4 `fake` adapter + YAML script format (§27.1); multiple fake ids.
- [x] M2.5 Fixture repo `sample_py` + patches (§27.2) + helper to materialize it in tmp with git.
- [x] M2.6 Workspace module: run branch, attempt worktree, diff capture (`DiffSummary` + patch), scope check, cleanup.
- [x] M2.7 `claude` adapter: probe via `--help` flag discovery, argv builder, stream-json parser, usage/session extraction, error classification; recordings in `tests/fixtures/recordings/claude/` (hand-authored from docs if CLI absent — mark `synthetic: true` in recording metadata); `docs/adapters/claude.md`.
- [x] M2.8 `codex` adapter: same deliverables as M2.7.
- [x] M2.9 Adapter test matrix (§10.5) for fake, claude, codex using fake binaries on PATH.
- [x] M2.10 CLI: `aix agent list|inspect|test|enable|disable`, `aix doctor`.
- [x] M2.11 Single-task orchestration: `aix run "<goal>" --agent <id>` → worktree → execute → diff → events → run branch commit; `tests/integration/test_single_task_run.py`.
- [x] M2.12 Run M2 Exit Gate; tag `m2-done`.

## M3 — Planning, routing, scheduling (§12–§14, §16)
- [x] M3.1 Repo facts inspector (languages, managers, commands, size) — shared with §17.1 detection.
- [x] M3.2 Intent engine (rules); risk keywords; tests.
- [x] M3.3 Skills: schemas for 4 files, loader, registry, 7 builtin skills, `aix skill list|inspect`.
- [x] M3.4 TemplatePlanner from skill `workflow.yaml`.
- [x] M3.5 AgentPlanner: prompt B.1, read-only run, JSON extraction, schema validation, 2-step repair loop, fallback to template; tests with fake `planner_output`.
- [x] M3.6 Plan post-processing: caps, capability normalization, implicit file-scope edges, verification spec defaults; `--plan-only`, `aix plan show`.
- [x] M3.7 Router (rules strategy): eligibility, scoring formula, penalties, static pins, fallbacks, reason codes; table tests incl. NO_ELIGIBLE_AGENT.
- [x] M3.8 Scheduler: ready set, `max_parallel`, dependency blocking, cancellation (`aix cancel`, SIGINT).
- [x] M3.9 Integration: serialized merges into run branch, conflict detection → MERGE_CONFLICT; `test_merge_conflict.py`.
- [x] M3.10 `gemini` adapter (as M2.7).
- [x] M3.11 `opencode` adapter (as M2.7).
- [x] M3.12 `aix status` live table.
- [x] M3.13 Golden G2, G5 passing (verification may be stubbed to "passed" behind a clearly named test-only flag removed in M4).
- [x] M3.14 Run M3 Exit Gate; tag `m3-done`.

## M4 — Verification (§17)
- [x] M4.1 Toolchain detection table + explicit command override; recorded in report.
- [x] M4.2 Check runner (allowlist, network deny best-effort, artifacts for stdout/stderr).
- [x] M4.3 Parsers: JUnit XML, SARIF, gitleaks JSON, pip-audit/npm-audit JSON.
- [x] M4.4 Check kinds: build/tests/lint/typecheck; missing tool → skipped → report incomplete.
- [x] M4.5 Security checks: secrets (gitleaks or builtin regex fallback — ADR), sast (semgrep/bandit when present), deps.
- [x] M4.6 Policy check (scope, forbidden files, large binaries).
- [x] M4.7 Baseline run + pre_existing handling.
- [x] M4.8 `ai_review` check: independent reviewer routing, B.3 prompt, findings JSON parse, error on unparseable.
- [x] M4.9 `VerificationReport.overall` rule with property tests.
- [x] M4.10 `aix verify`, `aix review`.
- [x] M4.11 Remove M3 verification stub flag; golden G1, G6, G7.
- [x] M4.12 Run M4 Exit Gate; tag `m4-done`.

## M5 — Decisions, Jev, retries, approvals (§18, §19, §20.4 partial)
- [x] M5.1 `DecisionState` builders per point with the §18.6 restrictions + tests (reject claim-sourced / long free text).
- [x] M5.2 Hard gates (§18.3) + exhaustive tests.
- [x] M5.3 DecisionService: gates → provider → outcome mapping → DecisionRecord with `inputs_hash`; provider fallback on error/timeout.
- [x] M5.4 `rules` provider for all decision points.
- [x] M5.5 `JevClient` protocol, `FakeJevClient`, real client using `typesafe-sdk` (optional extra; verify SDK surface by introspection; ADR with verified signature).
- [x] M5.6 Jev question catalog (Appendix A) + answer→outcome mappings + per-risk thresholds.
- [x] M5.7 Eval harness `aix dev eval-decisions` + ≥40 task_completion and ≥20 failure_triage labeled cases incl. adversarial; `--fail-under`; `--replay <bundle>` (bundle part completes in M7).
- [ ] M5.8 Failure classification (adapter patterns + generic); `failure_triage` point.
- [ ] M5.9 Retry mutations (§19.2) incl. non-consuming retries, identical-retry assertion, `split_task` via planner.
- [ ] M5.10 Escalation ladder (§19.3) + attempt/budget limits; `budget` decision point.
- [ ] M5.11 Approvals: model, `aix approvals|approve|deny`, TTY confirmation, token file, `AIX_AGENT_CONTEXT` guard, exit code 3 on wait, resume after grant.
- [ ] M5.12 `plan_review` for high-risk intents; `tool_risk` for control-plane-executed gated commands.
- [ ] M5.13 Golden G3, G4, G8, G10.
- [ ] M5.14 Run M5 Exit Gate; tag `m5-done`.

## M6 — Context fabric (§15)
- [ ] M6.1 Project facts from CLAUDE.md/AGENTS.md/CONTRIBUTING/README with caching on HEAD.
- [ ] M6.2 Handoff builder (facts + labeled claim).
- [ ] M6.3 Context pack assembly with token budget (tokenizer-free estimate: chars/4 — ADR) and priority truncation.
- [ ] M6.4 Prompt templates B.2/B.3 finalized + snapshot tests; prompt captured as artifact per attempt.
- [ ] M6.5 Compaction + `context.compacted` event.
- [ ] M6.6 `test_handoff.py` (G2 asserts agent B prompt contains agent A handoff).
- [ ] M6.7 Run M6 Exit Gate; tag `m6-done`.

## M7 — Artifacts, reports, observability, resume (§21, §22, §8.2)
- [ ] M7.1 Content-addressed artifact store + provenance.
- [ ] M7.2 Standard artifacts (plan, patches, verification, decision-log, agent-trace, manifest).
- [ ] M7.3 `report.md` + `report.html` (jinja2, deterministic) + snapshot tests; §23.3 summary on stdout.
- [ ] M7.4 Bundle export + `aix artifact verify`.
- [ ] M7.5 Cost accounting (reported vs estimated), price table in config; budget enforcement uses it.
- [ ] M7.6 `agent_stats` projection; router uses Bayesian observed rate; `aix stats agents`.
- [ ] M7.7 `aix trace`, `aix logs [--follow]`, `--json` everywhere.
- [ ] M7.8 Crash resume (`--resume`, INTERRUPTED) + `test_resume.py`; golden G9.
- [ ] M7.9 All golden G1–G10 green; run M7 Exit Gate; tag `m7-done`.

## M8 — Security hardening (§20)
- [ ] M8.1 Policy engine with versioned hash; permissions translation per adapter snapshot-tested.
- [ ] M8.2 Env filtering (base + manifest allowlist) + redactor applied to logs/streams/events/artifacts/prompts; leak test.
- [ ] M8.3 Agent-ineligibility when CLI cannot enforce required restriction for high-risk tasks.
- [ ] M8.4 Container sandbox mode (docker/podman), egress allowlist; tests skipped if no runtime.
- [ ] M8.5 `test_prompt_cannot_bypass_policy.py` (fake agent tries `aix approve`, reads token path, writes outside scope, edits `.aix/`).
- [ ] M8.6 `ollama` adapter (text-only, research/review/summarize).
- [ ] M8.7 `docs/security.md` (threat model, residual risks).
- [ ] M8.8 Run M8 Exit Gate; tag `m8-done`.

## M9 — API and plugins (§24, §25)
- [ ] M9.1 FastAPI app, endpoints, SSE events, token scopes, `aix serve` (127.0.0.1).
- [ ] M9.2 CLI/API equivalence contract test on G1.
- [ ] M9.3 Plugin entry points + manifest validation + semver range; builtins migrated; broken plugin reported by doctor.
- [ ] M9.4 Sample external plugin fixture (adapter + check) loaded in tests.
- [ ] M9.5 Run M9 Exit Gate; tag `m9-done`.

## M10 — Live validation and final report (§31)
- [ ] M10.1 Live tests per installed CLI (tiny prompt on scratch fixture copy); record fresh recordings via `aix dev record`.
- [ ] M10.2 Live Jev eval if key present; ADR with accuracy/calibration vs rules; do NOT change default provider unless criteria in §18.7 met.
- [ ] M10.3 DoD §31 walk-through with fake agents; attach bundle.
- [ ] M10.4 `FINAL_REPORT.md`: built vs planned, live vs fake-only matrix, open risks, ADR titles, how to run.
- [ ] M10.5 Run M10 Exit Gate; tag `m10-done`.

---

## Blocked
_(item id · problem · attempts made · stub left in place · revisit when)_

## Notes log
_(one line per completed item: `M1.4 — done; hypothesis found X, fixed`)_
M0.1 — done; all required tools present. Missing optional: podman, semgrep, gitleaks, pip-audit, TYPESAFE_API_KEY → container tests skip, security checks use fallbacks (§17.2/M4.5), Jev uses FakeJevClient. No ADR needed (nothing required missing).
M0.2 — done; .claude/ hooks+agent committed with the initial commit.
M0.3 — done; ADR-0002 records deps; typesafe-sdk resolves on PyPI (surface still to be verified in M5.5).
M0.4 — done; 32 packages + py.typed; agents/protocol.py, registry.py, subprocess.py, decision/service.py etc. are created in the milestones that implement them.
M0.5 — done; ruff/pyright configured in pyproject; 6 import contracts verified with deliberate violations (ADR-0003).
M0.6 — done; plugin in tests/aix_pytest_plugin.py (socket blocker, AIX_LIVE gate, anyio=asyncio); ADR-0004 drops pytest-asyncio. Coverage config present, threshold enforced from M3.
M0.7 — done; `make golden` tolerates pytest exit 5 (no tests) until G-scenarios land in M3.
M0.8 — done; `aix --version/--help` with smoke tests; committed together with M0.7 so every commit passes `make check`.
M0.9 — done; gate `make check && uv run aix --help && uv run lint-imports` green; tagged m0-done.
M1.1 — done; StrEnum values asserted against spec sets; ULID ids via Annotated patterns. DecisionOutcome.CHOOSE carries its arg in DecisionRecord.choice (ADR-0005 with M1.2). Added Capability vocabulary.
M1.2 — done; 12 models + supporting types, pyright-strict clean; shapes recorded in ADR-0005.
M1.3 — done; one error type per FailureClass (test asserts coverage) + classify(); internal errors mapped in module docstring.
M1.4 — done; 560 tests total incl. hypothesis state machines (task+run) and random-DAG blocked propagation; ADR-0006 lists added edges.
M1.5 — done; 22 schemas (snake_case names) committed; drift test in tests/contract; store event-payload schemas join in M1.6.
M1.6 — done; EventStore (aiosqlite, WAL, user_version migrations, append-only triggers, lock-serialized BEGIN IMMEDIATE appends); 32 payload models registered = §8.3 exactly and exported as schemas/event_*.json. Projection tables arrive in migration 002 (M1.7). Added StoreError (RESOURCE_FAILURE).
M1.7 — done; migration 002 + projections applied inside the append transaction; rebuild clears rows (DELETE, not DROP — equivalent, keeps DDL in migrations) and replays; replay-equality + corruption-repair + atomicity tests; typed readers on EventStore. agent_stats table exists but is populated in M7.6.
M1.8 — done; AixConfig schema (extra=forbid) + load_config with per-leaf sources; secret-looking keys refused in files; skill/task/cli layers take nested dicts (threshold names contain dots); `aix config show [--resolved] [--json] [--project]`. Caveat: pydantic-settings (ADR-0002) is not used by the loader — env is not a config layer in §9.
M1.9 — done; `aix init [--force] [--json] [--project]` creates .aix/, commented config, migrated DB, .gitignore entries, toolchain summary; non-git warns; ADR-0007.
M1.10 — done; gate green (make check; export-schemas + no drift; domain+store tests; init in fresh git repo); tagged m1-done.
M2.1 — done; AgentAdapter (runtime-checkable Protocol), AgentRequest/Event/Outcome/Handle/Permissions in agents/protocol.py.
M2.2 — done; agents/subprocess.py: spawn() async-context RunningProcess (own process group, exact env, line streaming + raw capture, 256KiB stderr ring, TERM->grace->KILL, group-wide cleanup, cancel from any task); ToolFailure if binary missing. 14 tests with tiny python scripts.
M2.3 — done; AdapterManifest, probe_binary (version + required-flag discovery, never paid), AdapterRegistry (importlib builtin discovery, enable/disable overlay, probe never raises), config.edit.set_agent_enabled persists enablement; ADR-0008 adds AgentSpec.health_reason. BUILTIN_IDS is empty until M2.4.
M2.4 — done; FakeAdapter + YAML FakeScript (events, apply_patch via git apply, write_files, write_outside_scope, claim, exit_code, stderr, sleep_s, usage, emit_findings, planner_output, failure); task type read from a 'TASK TYPE: <x>' prompt line (B.2 template must emit it, M6.4); attempts advance per script, last repeats; builtin 'fake' + make_fake_entry() for fake-a/fake-reviewer (fake and fake-* always enabled).
M2.5 — done; tests/fixtures/repos/sample_py (users store + handler, 5 baseline tests, ruff cfg) + 5 complete-diff patches (hello, jwt broken/fixed, planted AWS-doc fake key, outside-scope) + review findings JSON; tests/repos.py materializes it as a git repo. Fixtures are excluded from outer ruff/pyright/pytest collection.
M2.6 — done; tools/git.py (deterministic env: ignores user git config, fixed 'aix' identity), core/workspace/{scope,manager}.py: preflight (git repo + clean tracked tree, --allow-dirty snapshots via 'git stash create' without touching the user tree), run branch aix/run/<id>, attempt worktrees .aix/worktrees/<att> on aix/att/<att>, capture_diff (numstat -z + binary patch, sha256; user tree/branch never modified), glob scope check (.aix/.git always forbidden), commit_attempt, idempotent remove_workspace. Merge into run branch is M2.11/M3.9. core uses anyio.run_process because contract forbids core->agents.subprocess.
M2.7 — done; claude adapter: manifest, probe (real `claude --help` 2.1.283 verified flags), argv builder (prompt via stdin), stream-json parser, usage/session extraction, failure classifier, synthetic recordings (meta.yaml synthetic:true) + live tests (skipped w/o AIX_LIVE), docs/adapters/claude.md. Also: spawn() stdin is /dev/null unless stdin_data given; AgentRequest.stream_path added; agents/env.py builds minimal env + allowlist. Caveat: start/events/wait/cancel must run in one task.
M2.8 — done; codex adapter (real `codex exec --help` 0.147.0 verified flags; approval via -c approval_policy=never; write_scope not expressible -> documented for M8.3), synthetic recordings, parser, classifier, live tests, docs/adapters/codex.md. Refactor: shared agents/stream_adapter.py (StreamingCliAdapter) now backs claude and codex.
M2.9 — done; tests/adapter/test_matrix.py runs the 11 §10.5 rows identically against fake, claude, codex (rigs in tests/adapter_matrix.py); 2 explicit n/a (fake: malformed stream, permission flags) with reasons; a guard test keeps rows and reasons complete.
M2.10 — done; `aix agent list|inspect|test|enable|disable` (+ alias `aix agents`) and `aix doctor` (fail = python/git/config -> exit 5; everything else warns). `agent test` only probes unless --live or fake. Verified on this machine: real claude 2.1.283 and codex 0.147.0 both probe 'ready' (manifest required_flags exist). Tests use a hermetic PATH (tests/unit/conftest.py env fixture + tests/cli_env.py).
M2.11 — done; core/orchestrator/single.py + `aix run "<goal>" --agent <id> [--scope] [--allow-dirty] [--keep-worktrees] [--json]`: run branch -> worktree -> agent -> control-plane diff -> scope check -> accept/reject stand-in (replaced by M4 verification / M5 decisions) -> commit + checkout-free merge -> cleanup; full event trail, replay-equal projections, exit codes 0/1/2/5. Interim prompt (B.2 template in M6.4); fake agent scriptable via AIX_FAKE_SCRIPTS. Single attempt, no retries yet (max_attempts=1).
M2.12 — done; gate green (make check; tests/adapter 141 passed/2 documented n/a; test_single_task_run 13 passed; agent list ids ⊇ {claude,codex,fake}); tagged m2-done.
M3.1 — done; inspect_repo() in verification/detect.py returns new domain RepoFacts (ADR-0009, schema exported); reads markers/metadata only, command resolution stays M4.1.
M3.2 — done; core/intent/engine.py RulesIntentEngine (ordered kind rules, high/medium/low risk keywords, target path extraction); Jev classify hook deferred to M6 via the IntentEngine protocol.
M3.3 — done; skills/{schema,loader,registry}.py + builtin/<name>/ (4 files each, packaged data); workflow steps carry file_scope, capabilities and when_risk_at_least for the M3.4 TemplatePlanner; skill models are not in the domain schema export (skills-tier, not a wire type).
M3.4 — done; core/planner/{base,template}.py: Planner protocol, select_skill (kind + bugfix/write-tests goal heuristics, --skill override), risk-gated steps rewired past dropped ones; write tasks carry the skill verification spec (defaults added in M3.6).
M3.5 — done; ADR-0010 (key-based PlanDraft); core/planner/{draft,agent}.py + prompts/planner.j2 (snapshot-tested); 2 repairs then TemplatePlanner fallback; make_adapter_runner runs read-only in a throwaway worktree. Planner-run events and planner.provider selection land with M3.6 wiring.
M3.6 — done; ADR-0011; core/planner/{postprocess,select}.py, core/orchestrator/plan.py (plan_run), cli/plan.py. `auto` planner never picks builtin `fake`; --plan-only leaves the run `planned`. CAVEAT: CLI tests that plan must use tests/cli_env.hermetic — a first draft let the real `claude` CLI on this machine plan a test run (a live paid call, ~70s) before I noticed; fixed before commit.
M3.7 — done; ADR-0012; core/router/rules.py (pure route(RoutingContext) -> RoutingDecision, NoEligibleAgent with per-agent reasons); 28 table tests. Not yet wired into an orchestrator: the scheduler (M3.8) calls it and emits agent.selected.
M3.9 — done; core/orchestrator/executor.py execute_graph/execute_run (per-attempt worktrees, merges serialized by WorkspaceManager lock, conflict -> workspace.conflict + rebase_and_retry up to task.max_attempts, then STOP -> MERGE_CONFLICT; ADR-0013 adds integrating--stop-->failed). Acceptance is the STUB_VERIFICATION rule (in scope, write tasks change something), removed in M4.11.
M3.8 — done; ADR-0014; core/scheduler/engine.py (TaskDriver protocol; driver owns state), executor `_Driver`, cli/cancel.py + core/orchestrator/cancel.py; e2e tests send real SIGINT and run `aix cancel` against a live subprocess. Budget checks deferred to M5.10; handoff-in-prompt to M6 (so G2 in M3.13 asserts routing/independence only).
M3.10 — done; adapters/gemini/ (gemini-cli 0.55.1 installed here: probes ready; event schema read from its bundle source, no paid call). Recordings synthetic. Prompt on stdin with a fixed -p instruction; write mode = --approval-mode auto_edit (no shell approval; ADR-0015 with opencode). Resume by session id unverified live. Matrix now has 4 kinds; docs/adapters/gemini.md.
M3.11 — done; ADR-0015 (gemini+opencode discovered behavior, no --auto/yolo); adapters/opencode/ (opencode 1.18.26 installed here: probes ready; event types read from the binary handler; part field names unconfirmed live). No result event: completion = final step_finish. Recordings synthetic; matrix now 5 kinds.
M3.12 — done; core/orchestrator/status.py snapshot() (reused by the API in M9), cli/status.py: TTY redraws with rich Live, non-TTY `--watch` prints a frame only when the run changes; tested against a live slow subprocess run.
M3.13 — done; tests/golden/test_golden_multi.py. G2 asserts >=5 tasks, >=3 agents, independent reviewer, green integrated branch; its handoff-in-prompt assertion is M6.6. G5 shows independence also re-routes the review to the spare. Stub verification in use (removed M4.11).
M3.14 — done; exit gate green: make check (1215 passed, 10 live skipped), test_multi_task_run + test_merge_conflict (12 passed), golden G2/G5 (2 passed). Tagged m3-done.
M4.1 — done; verification/commands.py resolve_commands -> ResolvedCommand{argv,source,available}; python/node/go/rust then Makefile fallback; config wins. Recording in the report happens when the runner (M4.2/4.4) stores it in Check.command.
M4.2 — done; verification/runner.py run_command (own anyio runner: contract forbids agents.subprocess). Outcome statuses exited/timeout/blocked/tool_missing; stdout/stderr files in out_dir, bounded tails in memory. Network deny is proxies+offline env vars only (real enforcement = container sandbox, M8).
M4.3 — done; verification/parsers.py, ParseError on malformed input; junit rejects DTD/entities; gitleaks findings drop the secret text; pip-audit has no severity so every vuln is `high` (conservative; baseline M4.7 protects pre-existing ones).
M4.4 — done; verification/checks.py run_command_check -> CheckResult{check,outcome}. pytest gets --junitxml; exit 5 (no tests) = warning. Evidence ArtifactRefs stay empty until the artifact store (M7.1); output files are returned via outcome paths.
M4.5 — done; ADR-0016; verification/security.py. External tools (gitleaks/semgrep/bandit/pip-audit/npm audit) are exercised only through fake binaries (none installed here): real output shapes are unconfirmed until a machine has them.
M4.6 — done; verification/policy.py run_policy_check (reuses core scope glob rules; .env/.env.* except .example/.sample/.template/.dist, *.pem/*.key/*.p12/*.pfx/*.keystore, id_rsa, .aws/credentials; binary > 1 MiB = NUL in first 8 KiB).
M4.7 — done; ADR-0017; verification/baseline.py (Baseline model, run_baseline, apply_baseline). Wiring (run once before the first write attempt of a run, on the run-branch head) happens in the engine/orchestrator, M4.9-M4.11.
M4.8 — done; verification/ai_review.py + core/prompts/review.j2 (snapshot-tested; M6.4 finalizes B.2/B.3 wording). Reviewer runs via the same read-only throwaway-worktree runner as the planner; pick_reviewer reuses route() so independence is the §12 penalty. Optional by default (required only if the caller says so); unparseable reply = error.
M4.9 — done; hypothesis properties: order independence, monotonicity, required failure => failed, required skipped never passes, optional-only can only warn, AI review cannot rescue, report validator accepts only the computed overall. Rule itself was already implemented in M1.
M4.10 — done; verification/engine.py run_verification (single composition point, also for M4.11), tree.py (tracked diff + untracked files as added lines; .aix/ ignored), cli/verify.py. Found+fixed: aix-own .aix/ counted as a change. verify exits 1 for failed AND incomplete. Custom skill checks (skill verification.yaml `custom`) are not yet run by the engine.
M4.11 — done; ADR-0018. Executor runs run_verification per write attempt (baseline once per run, optional independent ai_review), STUB removed; tests/verif_env.py gives no-op commands for orchestrator tests, goldens G1/G6/G7 use the real toolchain. security/redact.py + EventStore redaction (found by G7: key leaked via the agent claim). Raw adapter stream files are not redacted yet (M8).
M4.12 — done; exit gate green: make check (1360 passed, 10 live skipped), tests/unit/verification + tests/integration/test_verification.py (149 passed), goldens G1/G6/G7 (3 passed). Tagged m4-done.
M5.1 — done; decision/state.py (pyright strict). Free text is blocked structurally: Label = ^[a-z0-9_:.-]{1,64}$, metric allowlist, builders take no ExecutionResult/claim (tested by signature inspection). Full suite now ~110s: run make check in the background.
M5.2 — done; decision/gates.py evaluate_gates(point, GateFacts) -> GateResult; BASE_OUTCOMES per §18.2. Precedence: verification gates, attempts, budget (narrows to stop/ask_human), then approval forcing ask_human only if still allowed. A forced reject cannot survive an exhausted budget. Exhaustive product test over 9 report kinds x flags.
M5.3 — done; decision/{provider,service}.py. Forced gate outcome skips providers (recorded as rules). Provider error/timeout/disallowed outcome/invalid choice -> fallback with `<name>:unavailable|outcome_not_allowed|invalid_choice`; nothing valid -> ask_human + decision:no_valid_answer. policy_version is an argument until the policy engine (M8.3) supplies its hash.
M5.4 — done; decision/providers/rules.py (table per point; _pick never leaves the allowed set). Added RoutingFacts/build_routing_state so the routing tie-break has candidates. Tool classes vocabulary: read_only, local_write, local_delete, git_push, deploy, db_migration_apply, secrets_write, external_network; unknown => ask_human.
M5.5 — done; ADR-0019 (SDK surface verified by introspection). decision/jev.py neutral types + FakeJevClient, providers/jev.py TypeSafeJevClient (only SDK importer; SDK failures -> JevError). Live test tests/live/test_jev_live.py skipped without AIX_LIVE/TYPESAFE_API_KEY; no key here so nothing was called against the real service.
M5.6 — done; decision/questions/{__init__,mapping}.py (A.1-A.6), thresholds.py (per-risk `<key>_high` override), providers/jev_provider.py. Jev low confidence / incomplete answers / points without a catalog entry defer to rules, keeping Jev questions+answers in the record. Extra default thresholds added to config (additive). FailureFacts gained `mutations`.
M5.7 — done; decision/eval.py + tests/decision/eval_cases/{task_completion (44), failure_triage (26)}.yaml (labels written from §18.4/§19.2, adversarial-tagged edge cases). rules = 70/70. `aix dev eval-decisions --provider rules|jev [--fail-under] [--replay records.json] [--save]`; jev needs AIX_LIVE=1 + TYPESAFE_API_KEY (not run: no key here). --replay takes a JSON list of DecisionRecords; bundle input arrives with M7.4. Default provider stays rules; enabling jev by default needs a live-eval ADR.
