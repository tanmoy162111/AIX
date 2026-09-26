# PROGRESS — aix build checklist

Legend: `[ ]` todo · `[x]` done · `[~]` partial (see Blocked) · IDs are stable: cite them in commits (`[M3.4, §12]`).
Rule: work the **first unchecked item of the current milestone**. A milestone is done only when its
PLAYBOOK §30 Exit Gate passes and `git tag m<N>-done` exists.

**Current milestone:** M1
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
- [ ] M1.6 SQLite store: migrations, WAL, append-only trigger on `events`, event payload schemas per type (§8.3).
- [ ] M1.7 Projections updated transactionally; `aix dev rebuild-projections`; replay-equality test.
- [ ] M1.8 Layered config loader (§9) with source tracking; `aix config show --resolved`.
- [ ] M1.9 `aix init` (creates `.aix/`, default config, gitignore entries, detects toolchain summary).
- [ ] M1.10 Run M1 Exit Gate; tag `m1-done`.

## M2 — Adapters, registry, single-task run (§10, §11, §14.3 partial, §23 partial)
- [ ] M2.1 `AgentAdapter` protocol, `AgentRequest/Event/Outcome/Handle`, `AgentPermissions`.
- [ ] M2.2 Shared subprocess runner: process groups, streaming, timeout→TERM→KILL, stderr ring buffer; tests with tiny scripts.
- [ ] M2.3 Adapter manifest schema + registry (discover via builtin list now; entry points in M9), health, enable/disable persisted in config.
- [ ] M2.4 `fake` adapter + YAML script format (§27.1); multiple fake ids.
- [ ] M2.5 Fixture repo `sample_py` + patches (§27.2) + helper to materialize it in tmp with git.
- [ ] M2.6 Workspace module: run branch, attempt worktree, diff capture (`DiffSummary` + patch), scope check, cleanup.
- [ ] M2.7 `claude` adapter: probe via `--help` flag discovery, argv builder, stream-json parser, usage/session extraction, error classification; recordings in `tests/fixtures/recordings/claude/` (hand-authored from docs if CLI absent — mark `synthetic: true` in recording metadata); `docs/adapters/claude.md`.
- [ ] M2.8 `codex` adapter: same deliverables as M2.7.
- [ ] M2.9 Adapter test matrix (§10.5) for fake, claude, codex using fake binaries on PATH.
- [ ] M2.10 CLI: `aix agent list|inspect|test|enable|disable`, `aix doctor`.
- [ ] M2.11 Single-task orchestration: `aix run "<goal>" --agent <id>` → worktree → execute → diff → events → run branch commit; `tests/integration/test_single_task_run.py`.
- [ ] M2.12 Run M2 Exit Gate; tag `m2-done`.

## M3 — Planning, routing, scheduling (§12–§14, §16)
- [ ] M3.1 Repo facts inspector (languages, managers, commands, size) — shared with §17.1 detection.
- [ ] M3.2 Intent engine (rules); risk keywords; tests.
- [ ] M3.3 Skills: schemas for 4 files, loader, registry, 7 builtin skills, `aix skill list|inspect`.
- [ ] M3.4 TemplatePlanner from skill `workflow.yaml`.
- [ ] M3.5 AgentPlanner: prompt B.1, read-only run, JSON extraction, schema validation, 2-step repair loop, fallback to template; tests with fake `planner_output`.
- [ ] M3.6 Plan post-processing: caps, capability normalization, implicit file-scope edges, verification spec defaults; `--plan-only`, `aix plan show`.
- [ ] M3.7 Router (rules strategy): eligibility, scoring formula, penalties, static pins, fallbacks, reason codes; table tests incl. NO_ELIGIBLE_AGENT.
- [ ] M3.8 Scheduler: ready set, `max_parallel`, dependency blocking, cancellation (`aix cancel`, SIGINT).
- [ ] M3.9 Integration: serialized merges into run branch, conflict detection → MERGE_CONFLICT; `test_merge_conflict.py`.
- [ ] M3.10 `gemini` adapter (as M2.7).
- [ ] M3.11 `opencode` adapter (as M2.7).
- [ ] M3.12 `aix status` live table.
- [ ] M3.13 Golden G2, G5 passing (verification may be stubbed to "passed" behind a clearly named test-only flag removed in M4).
- [ ] M3.14 Run M3 Exit Gate; tag `m3-done`.

## M4 — Verification (§17)
- [ ] M4.1 Toolchain detection table + explicit command override; recorded in report.
- [ ] M4.2 Check runner (allowlist, network deny best-effort, artifacts for stdout/stderr).
- [ ] M4.3 Parsers: JUnit XML, SARIF, gitleaks JSON, pip-audit/npm-audit JSON.
- [ ] M4.4 Check kinds: build/tests/lint/typecheck; missing tool → skipped → report incomplete.
- [ ] M4.5 Security checks: secrets (gitleaks or builtin regex fallback — ADR), sast (semgrep/bandit when present), deps.
- [ ] M4.6 Policy check (scope, forbidden files, large binaries).
- [ ] M4.7 Baseline run + pre_existing handling.
- [ ] M4.8 `ai_review` check: independent reviewer routing, B.3 prompt, findings JSON parse, error on unparseable.
- [ ] M4.9 `VerificationReport.overall` rule with property tests.
- [ ] M4.10 `aix verify`, `aix review`.
- [ ] M4.11 Remove M3 verification stub flag; golden G1, G6, G7.
- [ ] M4.12 Run M4 Exit Gate; tag `m4-done`.

## M5 — Decisions, Jev, retries, approvals (§18, §19, §20.4 partial)
- [ ] M5.1 `DecisionState` builders per point with the §18.6 restrictions + tests (reject claim-sourced / long free text).
- [ ] M5.2 Hard gates (§18.3) + exhaustive tests.
- [ ] M5.3 DecisionService: gates → provider → outcome mapping → DecisionRecord with `inputs_hash`; provider fallback on error/timeout.
- [ ] M5.4 `rules` provider for all decision points.
- [ ] M5.5 `JevClient` protocol, `FakeJevClient`, real client using `typesafe-sdk` (optional extra; verify SDK surface by introspection; ADR with verified signature).
- [ ] M5.6 Jev question catalog (Appendix A) + answer→outcome mappings + per-risk thresholds.
- [ ] M5.7 Eval harness `aix dev eval-decisions` + ≥40 task_completion and ≥20 failure_triage labeled cases incl. adversarial; `--fail-under`; `--replay <bundle>` (bundle part completes in M7).
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
