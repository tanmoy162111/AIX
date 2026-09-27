# aix — Final Report

Date: 2026-09-27 · Spec: `docs/playbook/PLAYBOOK.md` · Status: M0–M10 complete (tags `m0-done` … `m10-done`).

## 1. What was built

aix is a provider-agnostic control plane for coding agents. Given a goal it inspects the repo, plans a
task graph, routes tasks by capability to agents, runs each attempt in an isolated git worktree,
**verifies with executed checks** (never agent claims), has an independent agent review, makes every
accept/retry/escalate choice through a Decision Service (hard gates first, then a rules or Jev provider),
integrates onto `aix/run/<id>`, and emits a verifiable artifact bundle with a complete event trace.

| Milestone | Delivered |
|---|---|
| M0 | Repo scaffold, `make check`, import-linter layering, pytest plugin (socket blocker, live gate) |
| M1 | Domain models + JSON Schemas, state machines, typed errors, SQLite event store with projections, layered config, `aix init` |
| M2 | Adapter protocol, subprocess runner, `fake`/`claude`/`codex` adapters, worktree manager, single-agent `aix run`, adapter test matrix |
| M3 | Repo inspection, planner, capability router, multi-task orchestrator, handoffs, golden scenarios |
| M4 | Verification engine (build/tests/lint/types/security/secrets/policy), baselines, AI review |
| M5 | Decision Service, rules provider, Jev provider (against a fake client), gates, evaluation harness, retries |
| M6 | Context packs, prompt templates, approvals, plan review |
| M7 | Artifacts, bundles, reports, cost accounting, crash recovery/resume, agent stats |
| M8 | Policy engine, secret redaction, container sandbox, security bypass detection, `gemini`/`opencode`/`ollama` adapters, `docs/security.md` |
| M9 | HTTP API (`aix serve`, SSE, scoped tokens), CLI/API equivalence, entry-point plugins (adapters + checks) |
| M10 | Live validation, `aix dev record`, Definition-of-Done walk-through, this report |

Size: ~19.7k lines in `src/aix`, 1796 tests passing (`make check`), 13 golden tests (`make golden`).
Out of scope by spec §32: web dashboard, swarm mode, remote workers, cross-project memory, learned routing,
MCP hosting, auto-deploy.

## 2. Live vs fake-only matrix

Checked on 2026-09-27 on the build machine. "Live" here means one tiny read-only prompt (`AIX_LIVE=1`),
not a full multi-task run against a real agent.

| Component | Tested how | Result |
|---|---|---|
| claude 2.1.283 | recordings + fake binary; live probe + tiny prompt; **real captured stream** replayed by tests | live pass |
| codex 0.147.0 | same | live pass |
| opencode | same | live pass |
| ollama (`qwen2.5:0.5b`) | fake HTTP server; live tiny prompt | live pass |
| gemini | recordings + fake binary only | **live blocked**: Google rejects the account (`IneligibleTierError: UNSUPPORTED_CLIENT`, free tier of the CLI retired). aix now classifies this as `auth_failure`; the live test skips with the reason |
| Jev (typesafe-sdk via OpenRouter, `jev-1.13`) | `FakeJevClient` plus the 70-case live eval | **live eval run: 84.3% vs rules 100%** (ADR-0035); default stays `rules`. Direct TypeSafe key still untested |
| Container sandbox | real `docker` run with `bash:latest` when Docker is present | passes where Docker exists; no podman tested |
| Full multi-task run against real agents | fake agents (`test_dod.py`, G1–G10) | **not done live** |
| Failure-mode recordings (auth, rate limit, context, network) | hand-written from documented schemas (`meta.yaml` says `synthetic: true`) | not captured from real services |

Only the happy-path stream is real (`tests/fixtures/recordings/<agent>/live_read_only.jsonl`, sanitized by
`aix dev record`); every error classifier depends on synthetic streams plus the one real gemini failure.

## 3. Definition of Done (§31)

1. Milestones tagged; `make check` and `make golden` green (fresh-clone run not performed, only this working tree).
2. JWT run with fake agents: `tests/golden/test_dod.py` — 7 tasks, 3 agents, real toolchain checks, one retry
   after failed tests, 8 decisions, integration on the run branch, summary and bundle. Bundle attached:
   `docs/playbook/evidence/dod-jwt-run-bundle.zip` (verifies: 58 files).
3. Real agents: works by construction (`aix run`), but only the tiny-prompt live tests were executed. **Not
   demonstrated end to end with a real agent; no live run bundle is attached.**
4. Policy-gated actions need a human-channel approval: G4 and the M8 bypass tests.
5. Decision replay: the walk-through replays its own `decision-log.json` with 0 differences. Jev drift is
   reported separately; no replay against live Jev was done.

## 4. Open risks

- **Jev is below rules.** Live eval 59/70 (84.3%) vs 70/70, with non-monotonic calibration (ADR-0035), so it is
  opt-in only. Latency and cost were not analysed. The live run found and fixed a real bug in the triage question builder.
- **No end-to-end live run.** Agent behaviour under real multi-task load (long timeouts, tool prompts, rate
  limits mid-run) is only exercised through fakes and recordings.
- **Gemini adapter unverified live**, and Google has retired the CLI tier this account used.
- **Synthetic failure recordings** could drift from what the CLIs actually print; re-run `aix dev record`
  after CLI upgrades.
- **Container egress is not filtered** (`network: none` or unrestricted bridge); aix ships no image (ADR-0029).
- **Plugins are trusted in-process code**; `permissions` in manifests is declarative only (ADR-0031).
- **API is plain HTTP**, loopback by default; the approval token is a bearer secret (ADR-0030).
- Native write-scope enforcement is not expressible for codex; aix relies on post-hoc diff scope checks there.
- Live recordings contain a real session id and account rate-limit figures; review before publishing.

## 5. How to run

```bash
uv sync --all-extras
make check                 # gate: format, lint, types, layers, tests
make golden                # golden scenarios with fake agents
AIX_LIVE=1 make test-live  # live agents/Jev (skips what is missing or unauthenticated)
uv run aix init && uv run aix doctor
uv run aix run "Add JWT authentication to this repository"      # needs an enabled agent
uv run aix artifact export <run> --bundle && uv run aix artifact verify <zip>
AIX_LIVE=1 uv run aix dev record claude                          # refresh a recording
AIX_LIVE=1 OPENROUTER_API_KEY=... uv run aix dev eval-decisions --provider jev --save jev-eval.json   # or TYPESAFE_API_KEY
```

## 6. Architecture decisions

- ADR-0001: Seed decisions carried over from playbook v2
- ADR-0002: Dependency set and build backend
- ADR-0003: Interpretation of the §5.3 layering contract
- ADR-0004: Use anyio's pytest plugin; drop pytest-asyncio
- ADR-0005: Domain model shape where §6 leaves fields open
- ADR-0006: State machine edges added beyond the §7 diagram
- ADR-0007: `aix init` writes a commented config and extra .gitignore entries
- ADR-0008: `AgentSpec.health_reason` and manifest extensions
- ADR-0009: `RepoFacts` domain model for the repo-facts inspector
- ADR-0010: AgentPlanner wire format is a key-based `PlanDraft`, not a `TaskGraph`
- ADR-0011: `run.planned` records the planner and warnings; `auto` never picks the built-in `fake`
- ADR-0012: Router details the spec leaves open
- ADR-0013: Task edge `integrating --stop--> failed`; shared attempt helpers
- ADR-0014: `aix run` routes by default; `--agent` means single-task mode; cancellation transport
- ADR-0015: gemini and opencode adapter behavior discovered from the installed CLIs
- ADR-0016: Security checks: builtin secret scanner always on, network exceptions for audits
- ADR-0017: `pre_existing` is a `warning` check with a metric, not a new status
- ADR-0018: Verification gates acceptance; secrets are redacted centrally in the event store
- ADR-0019: Jev client: verified SDK surface and the neutral interface
- ADR-0020: Decision-driven retries in the executor
- ADR-0021: Approval resolution and resume
- ADR-0022: plan_review and tool_risk in the control plane
- ADR-0023: Context pack budgeting and token estimate
- ADR-0024: Artifact store, manifest and bundles
- ADR-0025: Reported-or-estimated cost and the price table
- ADR-0026: Crash recovery and `aix run --resume`
- ADR-0027: Policy engine, native-enforcement flags and the policy hash
- ADR-0028: Known-secret redaction registry
- ADR-0029: Container sandbox mode
- ADR-0030: HTTP API, token scopes and event streaming
- ADR-0031: Plugins via entry points, manifest-first activation
- ADR-0032: `aix dev record` and live-validation findings
- ADR-0033: Decision provider stays `rules`; live Jev eval not run
- ADR-0034: Jev via OpenRouter as a key fallback
- ADR-0035: Live Jev evaluation — default provider stays `rules`
