# aix — a control plane for coding agents

**Give it a goal. It plans, splits the work across the coding agents you already use (Claude Code,
Codex, OpenCode, Ollama, …), makes each one work in an isolated git worktree, *runs* the checks
instead of believing the agent, records every accept/retry/escalate decision, and hands you a
branch plus an audit trail.**

```bash
aix run "Add search and pagination to the users store, with tests and docs"
```

![aix control plane architecture](docs/diagrams/aix-architecture.png)

*Interactive version: [`docs/diagrams/aix-architecture.html`](docs/diagrams/aix-architecture.html).*

## Why

Coding agents are good at writing code and bad at grading themselves. "Done, all tests pass" is a
claim, not evidence, and once you use more than one agent you also need to decide who does what,
what happens after a failure, and what a human must approve. aix owns that layer so the agents don't
have to:

```text
Intent → Plan → Route → Execute → Verify → Decide → Artifact
```

## What has actually been run

Stated plainly, because a README about "evidence, not claims" should hold itself to that.

| | Result |
|---|---|
| Test suite | **1,818 tests** pass (unit, integration, adapter matrix, golden scenarios, property-based state machines); `pyright --strict` on the core; 6 import-layer contracts enforced |
| Real agents, end to end | **Two real runs.** Small: Claude planned and reviewed, Codex implemented and tested (3/3 tasks, $0.16, under 2 min). Larger multi-file feature (search + pagination, route, tests, docs): **three different agents in one run** (Claude plans and documents, Codex implements and tests, OpenCode reviews): 7/7 tasks, 0 retries, 58 checked tests, **$0.37, under 5 minutes**; the merged branch's tests pass when re-run independently. Evidence, including a verifiable bundle: [`docs/playbook/evidence/`](docs/playbook/evidence/) |
| Agent adapters, live | Claude Code, Codex CLI, OpenCode and Ollama pass live smoke tests. Gemini CLI is covered by recordings only (the test account was rejected by Google) |
| Definition-of-Done walk-through | A multi-agent JWT run with fake agents: 7 tasks, 3 agents, real checks, one retry after failing tests, a verifiable bundle, and decision replay with zero differences ([`tests/golden/test_dod.py`](tests/golden/test_dod.py)) |

The real runs were worth doing: they exposed four bugs that fake agents could never have shown (two
false "tampering" alarms, an always-on stub agent that received real tasks, and bytecode files getting
committed). They are fixed and explained in [ADR-0036](docs/playbook/DECISIONS.md) and
[ADR-0040](docs/playbook/DECISIONS.md). A real Gemini auth failure was also handled correctly: the run
switched agents and finished.

## What it does

- **Verification is evidence.** Build, tests, lint, type checks, secret/SAST/dependency scans and
  policy checks are executed by aix. An agent's claim counts for nothing.
- **Independent review.** A different agent reviews the change, so the author never grades itself.
- **Isolation.** Every attempt runs in its own git worktree. Results land on `aix/run/<id>`; you
  merge. Your working tree and branch are never touched.
- **Recorded decisions.** Hard gates first (a failed required check can never be accepted), then a
  provider. Every decision stores its inputs, answer and reason codes, so it can be replayed.
- **Retries with a reason.** Failures are classified and each retry changes something (failure
  context, scope reminder, a different agent, a smaller prompt). Identical retries are refused.
- **Human approval where it matters.** High-risk plans and gated commands wait for `aix approve`,
  which needs a TTY or a token and refuses to run inside an agent.
- **A trail you can check.** Plan, prompts, agent streams, patches, verification output and the
  decision log are content-addressed and exported as a bundle that `aix artifact verify` re-hashes.
- **Budgets and recovery.** Cost, attempt and wall-clock limits per run; `aix run --resume` recovers
  a run whose orchestrator died.
- **Extensible.** Adapters and checks are plugins discovered through entry points, with a manifest
  validated before any plugin code is imported. There is also an HTTP API (`aix serve`, SSE events,
  scoped tokens, loopback by default).

## Where it sits among similar tools

Multi-agent coding orchestrators are a busy space, and aix is not the first thing to run several
agents in parallel. Tools differ in what they emphasise. aix emphasises **independent, executed
verification as the source of truth**, an **append-only event log** you can replay, **approvals an
agent cannot grant to itself**, and a **provider-agnostic core** (an import-linter contract keeps
vendor code out of it). If you mainly want agents running side by side with a dashboard for reading
diffs, other tools may fit better. aix is a CLI first, and it has no web UI.

## Quick start

Requires Python 3.12+, git and [uv](https://docs.astral.sh/uv/). aix is not published to PyPI yet;
install from source:

```bash
git clone https://github.com/tanmoy162111/AIX.git
cd AIX
uv sync --all-extras
uv run aix doctor          # which agent CLIs, tools and keys it can see
```

Agent CLIs are optional and use *your* existing logins; aix needs no API key of its own. In your
project (any git repository):

```bash
uv run aix init                                   # .aix/ with a commented config
uv run aix run "Add a retry option to the HTTP client" --plan-only   # see the plan first
uv run aix run "Add a retry option to the HTTP client"               # route, execute, verify
uv run aix trace <run-id>                         # tasks, attempts, checks, decisions
git merge aix/run/<run-id>                        # you decide what lands
```

Useful flags: `--budget-usd`, `--max-parallel`, `--scope <glob>`, `--agent <id>` (single-agent mode),
`--decision-provider rules|jev`, `--json`. Exit codes: `0` success, `1` failed, `3` waiting for
approval, `4` cancelled, `6` budget exceeded. Enable agents in `.aix/config.yaml` (`agents.enabled`).

| Command | Purpose |
|---|---|
| `aix run` / `status` / `trace` / `logs` / `cancel` | Execute and observe runs |
| `aix plan show` | Inspect a recorded plan |
| `aix verify` / `aix review` | Verify or independently review the current tree |
| `aix approvals` / `approve` / `deny` | Human decisions on gated steps |
| `aix artifact export --bundle` / `verify` | Export and check an audit bundle |
| `aix agent list` / `doctor` / `stats` | Inspect agents, environment and observed performance |
| `aix serve` | HTTP API with SSE events |
| `aix dev record` / `eval-decisions` | Refresh recordings; evaluate decision providers |

## Decisions and Jev

By default decisions come from a **rule-based provider**. Optionally,
[Jev](https://openrouter.ai/docs/guides/community/jev) (TypeSafe AI's typed "System One" model,
which returns a choice with confidence instead of text) can act as an advisor for narrow questions
such as "accept, retry, or switch agent?". Gates always run first; Jev sees only verified facts, never
agent prose; low confidence or an outage falls back to the rules and the record says so.

```bash
uv sync --extra jev
export OPENROUTER_API_KEY=...     # or TYPESAFE_API_KEY; never commit keys
uv run aix run "..." --decision-provider jev
```

**Measured, not assumed:** on the 70 labelled decision cases, live Jev scores **90.0%** against
100% for the rules provider (its misses are mostly asking a human on high-risk work, the cautious
direction), and its confidence is not well calibrated. So **`rules` stays the default and Jev is
opt-in.** Caveat: the cases are hand-labelled from the rules' own policy table, so the rules' 100% is
consistency, not independent accuracy. Details in
[ADR-0035](docs/playbook/DECISIONS.md) and [ADR-0039](docs/playbook/DECISIONS.md).

## Security, honestly

A policy engine, central secret redaction, tool-call inspection, approval gating and an optional
container sandbox reduce the blast radius. In the default `local` mode aix **cannot contain a
malicious agent**: it runs as your user and detection happens after the fact. Container mode limits
the filesystem, but network egress is either fully off or unrestricted, not filtered. Run bundles
have machine details stripped from agent streams but still contain what agents said and did, so read
one before you share it. Full threat model:
[`docs/security.md`](docs/security.md).

## Limitations

- Real-agent coverage is still thin: two small-to-medium tasks, plus live smoke tests. Long tasks and
  rate limits mid-run are exercised through fakes and recordings only. A slow model can burn a whole
  attempt timeout (one OpenCode review did).
- Gemini CLI has no live proof. Failure-mode recordings (auth, rate limit) are hand-written from
  documented schemas, not captured.
- Jev is below the rules provider on the suite; no web UI; not on PyPI.

## Repository layout

```text
src/aix/
  domain/         pure Pydantic models, enums, state machines (imports nothing else)
  core/           planner, router, scheduler, executor, retries, context fabric
  decision/       gates, rules provider, Jev provider, evaluation harness
  verification/   check runner, parsers, security checks, AI review
  agents/         adapter protocol + adapters (fake, claude, codex, gemini, opencode, ollama)
  artifacts/      content-addressed object store, bundles, reports
  security/       policy, redaction, approvals, sandbox
  store/          append-only SQLite event store and projections
  plugins/        entry-point discovery and manifest validation
  api/ cli/       FastAPI service and Typer commands
tests/            unit, integration, adapter, golden scenarios, live (opt-in)
docs/playbook/    PLAYBOOK (spec), DECISIONS (39 ADRs), PROGRESS, FINAL_REPORT, evidence/
```

## Development

```bash
make check                  # format, lint, typecheck, import layers, tests
make golden                 # end-to-end golden scenarios with fake agents
AIX_LIVE=1 make test-live   # live agent/Jev tests (needs the CLIs and keys; costs money)
```

The default test suite never calls a paid agent or network service.

## How it was built

aix was built almost entirely by an AI coding agent working autonomously under a written operating
contract: a [spec](docs/playbook/PLAYBOOK.md), a milestone checklist with exit gates, an append-only
[decision log](docs/playbook/DECISIONS.md), and a rule that nothing counts as done until `make check`
is green. All eleven milestones (M0–M10) are tagged. The full account, including what runs live and
what is fake-only, is in [`docs/playbook/FINAL_REPORT.md`](docs/playbook/FINAL_REPORT.md).
