# aix — Universal AI Agent Control Plane
## Developer Playbook v2 (autonomous build edition)

**Audience:** Claude Code, building this system end-to-end without human intervention.
**Companion files:** `CLAUDE.md` (operating contract), `PROGRESS.md` (checklist), `DECISIONS.md` (ADR log).
**Status of this document:** normative. "MUST" = required for the milestone gate. "SHOULD" = default unless an ADR says otherwise. "MAY" = optional.

---

## 0. How to read this

- §1–§3: what we are building and what changed from v1 of the playbook.
- §4–§9: foundations (stack, layout, schemas, state machines, storage, config).
- §10–§22: subsystems, in the order they are built.
- §23–§25: interfaces (CLI, API, plugins).
- §26–§29: testing, fakes, golden scenarios, performance.
- §30–§32: milestones with executable exit gates, Definition of Done, deferred scope.
- Appendices: Jev question catalog, prompt templates, adapter invocation reference.

Every milestone in §30 lists the sections it implements. Build strictly in milestone order.

---

## 1. Product definition

aix is a **control plane** that sits above existing AI coding/research agents (Claude Code, Codex CLI,
Gemini CLI, OpenCode, local models) and coordinates them to produce **verified, traceable work**.

Pipeline: **Intent → Plan → Route → Execute → Verify → Decide → Artifact**.

- Agents do work. Tools give capabilities. Skills define reusable workflows.
- The control plane owns orchestration, state, policy, and evidence.
- Verification is executed checks, never agent claims.
- Decisions are typed, recorded, and reproducible. Jev is the primary *judgment* provider; policy
  code is the *authority*.
- Artifacts carry provenance: who, what, which inputs, which checks, which decisions.

Thesis to prove with the MVP: *one CLI command can take a coding goal, split it across heterogeneous
agents, verify the result independently, make explicit recorded decisions (retry/accept/escalate),
and emit an evidence bundle — with no manual coordination.*

Primary interface: CLI (`aix`). API and dashboard are later clients of the same core.

---

## 2. Terminology (strict)

| Term | Definition | Answers |
|---|---|---|
| **Run** | One invocation of a user goal (`aix run "..."`). Owns a task graph. | What was asked? |
| **Task** | A node in the graph: one unit of work with a type, required capabilities, dependencies, verification spec. | What must be done? |
| **Attempt** | One try at a task by one agent. A task has 1..N attempts. | Which try is this? |
| **Execution** | The process-level record of an attempt (command, pid, exit code, stream, cost). | What actually ran? |
| **Agent** | An execution backend (Claude Code, Codex, …). Reached only through an Adapter. | Who does the work? |
| **Adapter** | Translation layer between the control plane and one agent. Owns all vendor specifics. | How do we talk to it? |
| **Tool** | A capability an agent or verifier can use (git, shell, docker, semgrep, MCP server). | What can be touched? |
| **Skill** | Declarative, agent-agnostic workflow definition (task template + instructions + verification). | What kind of work? |
| **Workspace** | The isolated filesystem an attempt runs in (a git worktree in MVP). | Where does work happen? |
| **Check** | One executed verification step producing evidence. | Did one thing pass? |
| **Verification Report** | All checks for an attempt plus an overall status. | Did the work succeed? |
| **Decision** | A typed, recorded outcome from the Decision Service (accept/retry/…). | What happens next? |
| **Decision Provider** | An implementation behind the Decision Service: `rules` (default), `jev`, `fake`. | Who judges? |
| **Policy** | Declarative rules that constrain actions; always enforced in code; cannot be overridden by a decision provider. | What is allowed? |
| **Approval** | A human grant for a gated action, recorded with actor and scope. | Did a human OK this? |
| **Artifact** | A durable, content-addressed output with provenance. | What proves it? |
| **Event** | An append-only record of something that happened. The source of truth for state. | When/what happened? |
| **Plugin** | A packaging/extension mechanism (Python entry point) that contributes adapters, tools, skills, checks, exporters, or decision providers. | How is it extended? |

Do not conflate these. Code names mirror these terms exactly.

---

## 3. What changed from playbook v1 (gaps filled)

v1 was a good architectural vision but not executable. v2 adds:

1. **Concrete stack and layout** (§4–§5) with an enforced layering contract.
2. **Jev modeled correctly.** Jev (TypeSafe AI) is a *System One* model: it takes a **state** and a set
   of **typed questions** — `Choice` (pick one of ≤255 options), `Score` (position on 2–10 ordered
   levels), `Noul` (probability a yes/no statement is true) — and returns typed answers with
   calibrated confidence and full distributions in one parallel pass. It does not generate text,
   cannot explain, reads instructions literally, degrades with bloated state, and does **not** treat
   state as hostile. v1's `evaluate(condition="implementation is production ready")` is the wrong
   shape. v2 (§18) builds a compact, structured, sanitized state from *verified facts*, asks typed
   questions, applies per-decision thresholds in code, and keeps hard gates in code.
3. **Workspace isolation**: git worktree per attempt, explicit merge step, conflict handling (§14).
4. **Agent output contract**: the control plane computes the diff itself; agent prose is a *claim*
   stored for review, never evidence (§10.4).
5. **Deterministic fakes** (`fake` agent, `FakeJevClient`, fixture repo) so the whole system is
   buildable and testable offline, and golden scenarios run in CI (§27–§28).
6. **Event-sourced state** with SQLite, projections, and resumability after crash (§8).
7. **Explicit failure taxonomy → retry mutation mapping** (§19).
8. **Honest threat model** for local vs. container execution; approval integrity (§20).
9. **Planner robustness**: schema-constrained LLM planning with repair loop and a template fallback (§13).
10. **Executable milestone gates** instead of prose acceptance criteria (§30).

---

## 4. Technology stack and conventions

| Concern | Choice | Notes |
|---|---|---|
| Language | Python ≥3.12 | |
| Packaging | `uv`, `pyproject.toml`, src layout | extras: `jev`, `api`, `security`, `dev` |
| CLI | `typer` + `rich` | |
| Schemas | `pydantic` v2 | JSON Schema exported to `schemas/` |
| Async | `anyio` | subprocesses via `anyio.open_process` |
| Storage | SQLite via `aiosqlite`, WAL mode | one DB per project: `.aix/aix.db` |
| Config | YAML (`pyyaml`) + pydantic-settings | layered, §9 |
| Templates | `jinja2` | prompts + HTML reports |
| Logging | `structlog` → JSON lines | secrets redacted, §20.5 |
| HTTP (API, M9) | `fastapi` + `uvicorn`, SSE for streams | |
| Jev | `typesafe-sdk` (optional extra `jev`) | wrapped behind `JevClient` protocol |
| Tests | `pytest`, `pytest-asyncio` (anyio), `hypothesis`, `syrupy` (snapshots) | |
| Lint/format | `ruff` | |
| Types | `pyright` (strict on core/domain/decision) | |
| Layering | `import-linter` | §5.3 |
| Build tasks | `Makefile` | targets listed in CLAUDE.md §6 |

Conventions: IDs are prefixed ULIDs (`run_01J…`, `task_…`, `att_…`, `exe_…`, `chk_…`, `dec_…`,
`apv_…`, `art_…`, `evt_…`). Times are UTC ISO-8601. Hashes are SHA-256 hex. All enums are
lowercase snake_case strings.

---

## 5. Repository layout

### 5.1 Tree

```text
aix/
├── CLAUDE.md
├── Makefile
├── pyproject.toml
├── .importlinter
├── schemas/                      # generated JSON Schemas (committed; CI checks drift)
├── docs/
│   ├── playbook/                 # PLAYBOOK.md, PROGRESS.md, DECISIONS.md, FINAL_REPORT.md
│   ├── architecture.md           # generated/maintained overview
│   └── adapters/<id>.md          # per-adapter notes incl. discovered CLI behavior
├── src/aix/
│   ├── domain/                   # pure models, enums, errors, state machines. No I/O.
│   ├── store/                    # event store, projections, migrations
│   ├── config/                   # layered config loader, schemas
│   ├── core/
│   │   ├── intent/               # goal → Intent
│   │   ├── planner/              # Intent → TaskGraph (llm + template planners)
│   │   ├── router/               # Task → RoutingDecision
│   │   ├── scheduler/            # graph execution, concurrency, dependencies
│   │   ├── orchestrator/         # run lifecycle, attempt loop, retries
│   │   ├── workspace/            # git worktrees, diff capture, merge
│   │   └── context/              # context fabric, compaction, handoff
│   ├── agents/
│   │   ├── protocol.py           # AgentAdapter protocol + result types
│   │   ├── registry.py           # discovery, health, enable/disable
│   │   ├── subprocess.py         # shared streaming subprocess runner
│   │   └── adapters/{fake,claude,codex,gemini,opencode,ollama}/
│   ├── skills/                   # registry, loader, builtin/*
│   ├── tools/                    # tool registry: shell, git, fs, docker, mcp (later)
│   ├── verification/             # engine, check runners, toolchain detection, reviewers
│   ├── decision/
│   │   ├── service.py            # DecisionService
│   │   ├── gates.py              # hard policy gates (code, non-overridable)
│   │   ├── providers/{rules,jev,fake}.py
│   │   └── questions/            # Jev question catalog (Appendix A)
│   ├── security/                 # policy engine, sandbox, secrets, approvals
│   ├── artifacts/                # store, provenance, renderers, exporters
│   ├── observability/            # tracing, metrics, cost
│   ├── plugins/                  # entry-point loading
│   ├── api/                      # FastAPI app (M9)
│   └── cli/                      # typer app; thin, calls core services only
└── tests/
    ├── unit/  integration/  adapter/  golden/  live/
    └── fixtures/
        ├── repos/sample_py/      # fixture repository (§27.2)
        ├── agent_scripts/        # fake-agent scenario scripts (§27.1)
        └── recordings/<agent>/   # captured real CLI streams for parser tests
```

### 5.2 Runtime directory (inside the user's project)

```text
.aix/
├── config.yaml
├── aix.db                        # events + projections
├── worktrees/<attempt_id>/       # isolated workspaces (gitignored)
├── runs/<run_id>/                # per-run logs, stream captures, prompts sent
└── artifacts/
    ├── objects/<sha256[:2]>/<sha256>   # content-addressed blobs
    └── bundles/<run_id>.zip
```
`aix init` adds `.aix/worktrees/` and `.aix/runs/` to `.gitignore`.

### 5.3 Layering contract (enforced by import-linter)

```text
cli, api            → may import anything below
core, verification, decision, artifacts, security, observability, skills, tools
                    → may import domain, store, config, agents.protocol, agents.registry
agents.adapters.*   → may import domain, agents.protocol, agents.subprocess, config. NOTHING in core.
domain              → imports nothing from aix except domain
```
Forbidden (contract must fail the build): `aix.core` → `aix.agents.adapters`; `aix.decision` →
`typesafe_sdk` outside `decision/providers/jev.py`; any module → `aix.cli`.

---

## 6. Domain model (MUST be implemented in `aix.domain` as Pydantic models)

Only key fields shown; add fields via ADR. All models `frozen=True` except where noted; state changes
happen by emitting events and rebuilding projections.

```python
class Run(BaseModel):
    id: RunId; project_root: Path; goal: str
    intent: Intent | None; graph_id: GraphId | None
    status: RunStatus                 # §7.1
    budget: Budget                    # max_cost_usd, max_attempts_total, max_wall_seconds
    created_at: datetime; finished_at: datetime | None

class Intent(BaseModel):
    goal: str
    kind: Literal["coding","research","review","security","devops","docs","other"]
    risk: Literal["low","medium","high"]
    constraints: list[str]            # user/extracted constraints, literal text
    target_paths: list[str]           # hints, may be empty

class Task(BaseModel):
    id: TaskId; run_id: RunId; title: str; goal: str
    type: TaskType                    # inspect|research|design|implement|test|review|security_review|document|integrate
    skill: str | None
    required_capabilities: list[Capability]
    depends_on: list[TaskId]
    file_scope: list[str]             # globs the task is expected to touch; [] = read-only
    verification: VerificationSpec    # required + optional checks
    risk: Literal["low","medium","high"]
    max_attempts: int = 3
    status: TaskStatus                # §7.2

class TaskGraph(BaseModel):
    id: GraphId; run_id: RunId; tasks: list[Task]
    # invariants (validated): acyclic; ids unique; deps exist; at least one task;
    # tasks with overlapping file_scope that are not ordered by deps get an implicit edge (§14.3)

class AgentSpec(BaseModel):           # from adapter manifest + probe
    id: str; name: str; kind: Literal["cli","api","local"]
    version: str | None
    capabilities: dict[Capability, float]   # 0..1 self-declared prior strength
    supports: AgentSupports           # streaming, sessions, non_interactive, cancel, cost_reporting, model_select
    models: list[str]; default_model: str | None
    cost_class: Literal["free","low","medium","high"]
    health: Literal["ready","degraded","unavailable","disabled"]

class Attempt(BaseModel):
    id: AttemptId; task_id: TaskId; number: int
    agent_id: str; model: str | None
    mutation: RetryMutation | None    # why this attempt differs from the previous (§19.2)
    workspace: Path; base_commit: str
    status: AttemptStatus

class ExecutionResult(BaseModel):
    attempt_id: AttemptId; exit_code: int | None
    status: Literal["completed","failed","timeout","cancelled"]
    failure: FailureClass | None
    claim: str | None                 # agent's final message; NOT evidence
    diff: DiffSummary                 # computed by control plane from workspace
    tool_calls: list[ToolCallRecord]
    usage: Usage                      # input/output tokens, cost_usd (nullable), model
    duration_ms: int; stream_path: Path

class Check(BaseModel):
    id: CheckId; kind: CheckKind      # build|tests|lint|typecheck|security_sast|secrets|deps|ai_review|policy|custom
    status: Literal["passed","failed","warning","skipped","error"]
    severity: Literal["info","low","medium","high","critical"]
    required: bool
    summary: str                      # machine-generated, factual
    metrics: dict[str, float]         # e.g. tests_total, tests_failed, findings_high
    evidence: list[ArtifactRef]       # logs, junit xml, sarif
    command: list[str] | None; duration_ms: int

class VerificationReport(BaseModel):
    attempt_id: AttemptId; checks: list[Check]
    overall: Literal["passed","failed","warning","incomplete"]
    # overall rule: any required failed/error → failed; any required skipped → incomplete;
    # else any warning or optional failure → warning; else passed.

class DecisionRecord(BaseModel):
    id: DecisionId; point: DecisionPoint   # §18.2
    subject: str                           # task/attempt/tool-call id
    provider: Literal["rules","jev","fake","human"]
    gate_result: GateResult                # hard gates evaluated first (§18.3)
    state: dict                            # exact state sent to the provider (sanitized)
    questions: dict                        # exact question schema sent
    answers: dict                          # typed answers incl. confidence + distribution
    outcome: DecisionOutcome               # accept|retry|reject|escalate|switch_agent|ask_human|stop|allow|deny|choose:<x>
    reason_codes: list[str]                # e.g. ["gate:required_check_failed", "jev:confidence_below:0.85"]
    provider_meta: dict                    # model id/version, latency_ms, cost_usd
    inputs_hash: str                       # sha256 of canonical(state, questions, policy version)

class Approval(BaseModel):
    id: ApprovalId; subject: str; action: str; scope: dict
    requested_at: datetime; status: Literal["pending","granted","denied","expired"]
    actor: str | None; channel: Literal["cli_tty","api_token"] | None; decided_at: datetime | None

class Artifact(BaseModel):
    id: ArtifactId; type: ArtifactType; media_type: str
    sha256: str; size: int; path: Path     # path in object store
    provenance: Provenance                 # §21.2
```

Export every model's JSON Schema to `schemas/<name>.json` via `aix dev export-schemas`; a test fails
if the committed schema differs from the generated one.

---

## 7. State machines (in `aix.domain.state`, table-driven, exhaustively tested)

### 7.1 Run
```text
created → planning → planned → executing → finalizing → completed
                 ↘ failed      ↘ waiting_approval ↗ (back to executing)
any non-terminal → cancelled   ;   executing → failed (budget/stop decision)
```

### 7.2 Task
```text
created → ready (deps satisfied) → assigned → running → verifying → deciding
deciding --accept--> accepted → integrating → completed
deciding --retry / switch_agent / escalate--> ready (new attempt, mutation recorded)
deciding --ask_human--> waiting_approval --granted--> ready | --denied--> failed
deciding --reject / stop--> failed
any → blocked (a dependency failed)  ;  any non-terminal → cancelled
```
Rules:
- Transitions happen only via `transition(task, event) -> Task | IllegalTransition`.
- `hypothesis` state-machine test: no sequence of events yields an undefined state; terminal states
  (`completed`, `failed`, `cancelled`) absorb.
- A task whose dependency ends `failed`/`cancelled` becomes `blocked`, and a `blocked` task never runs.

---

## 8. Event store and persistence

### 8.1 Schema (migrations in `store/migrations/NNN_*.sql`)

```sql
CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT UNIQUE NOT NULL,           -- evt_...
  run_id TEXT, task_id TEXT, attempt_id TEXT,
  type TEXT NOT NULL,                -- e.g. task.state_changed
  ts TEXT NOT NULL,
  payload TEXT NOT NULL,             -- JSON, schema per type
  schema_version INTEGER NOT NULL
);
CREATE INDEX events_run ON events(run_id, seq);
-- projections (rebuildable from events)
CREATE TABLE runs(...); CREATE TABLE tasks(...); CREATE TABLE attempts(...);
CREATE TABLE checks(...); CREATE TABLE decisions(...); CREATE TABLE approvals(...);
CREATE TABLE artifacts(...); CREATE TABLE agent_stats(...);  -- §22.3
```

### 8.2 Rules
- Events are append-only. No UPDATE/DELETE on `events` (enforce with a trigger that raises).
- Projections are updated in the same transaction as the event insert.
- `aix dev rebuild-projections` drops projections and replays events; a test asserts equality.
- High-volume agent stream output is **not** stored as events; it is written to
  `.aix/runs/<run>/<attempt>.stream.jsonl` and referenced by path + hash. Only summarized
  `agent.output` milestones go into events (rate-limited, ≤ 1 per second per attempt).
- **Resumability:** on `aix run --resume <run_id>` (or startup detecting an unfinished run), attempts
  in `running` whose process is gone become `failed` with `FailureClass.INTERRUPTED`, then normal
  retry logic applies. Accepted-but-not-integrated tasks resume at integration.

### 8.3 Event types (minimum)
`run.created|planned|state_changed|completed|failed|cancelled`,
`task.created|state_changed`, `attempt.created|started|finished`,
`agent.selected|output|tool_called|failed`, `workspace.created|merged|conflict|removed`,
`check.started|finished`, `verification.completed`,
`decision.requested|completed`, `approval.requested|granted|denied|expired`,
`policy.violation`, `artifact.created`, `budget.exceeded`, `context.compacted`.

---

## 9. Configuration

Layers (later overrides earlier): built-in defaults → `~/.config/aix/config.yaml` (user) →
`<project>/.aix/config.yaml` (project) → skill defaults → task overrides → CLI flags.
`aix config show --resolved` prints the merged config with the source of each key.

```yaml
# .aix/config.yaml (defaults shown)
agents:
  enabled: [claude, codex, gemini, opencode]   # fake is always available in tests
  overrides:
    codex: { model: null, timeout_s: 1800 }
routing:
  strategy: capability            # capability | static | jev_assisted
  static: {}                      # optional pins: { implement: codex }
  prefer_independent_reviewer: true
planner:
  provider: auto                  # auto | agent:<id> | template
  max_tasks: 12
execution:
  max_parallel: 3
  attempt_timeout_s: 1800
  workspace: worktree             # worktree | inplace (inplace only for read-only runs)
verification:
  required_default: [build, tests, lint]
  commands: {}                    # explicit overrides: { tests: ["pytest","-q"] }
  security: { sast: auto, secrets: auto, deps: auto }   # auto = run if tool installed
decision:
  provider: rules                 # rules | jev | fake
  jev:
    model: null                   # pin a model/version when known; record in ADR
    timeout_ms: 3000
    on_error: fallback_rules      # never block a run because Jev is down
  thresholds:                     # per decision point, §18.5
    task_completion.accept_confidence: 0.85
    failure_triage.min_confidence: 0.6
    tool_risk.deny_if_p_risky_above: 0.3
budget:
  max_cost_usd_per_run: 10.0
  max_attempts_per_run: 20
  max_wall_seconds_per_run: 7200
security:
  sandbox: local                  # local | container
  network: { agents: provider_default, checks: deny }
  shell_allow: [git, python, pytest, ruff, mypy, npm, node, pnpm, yarn, go, cargo, make, uv]
  approval_required_for: [deploy, db_migration_apply, secrets_write, push, delete_outside_scope]
artifacts:
  bundle: true
  formats: [json, md, html]
```

Secrets are never read from config files; only from env or OS keychain (§20.5).

---

## 10. Agent adapter protocol

### 10.1 Interface (`aix.agents.protocol`)

```python
class AgentAdapter(Protocol):
    id: str
    async def probe(self) -> AgentSpec: ...                     # binary present? version? auth? capabilities
    async def start(self, req: AgentRequest) -> AgentHandle: ... # spawn; returns immediately
    def events(self, h: AgentHandle) -> AsyncIterator[AgentEvent]: ...  # normalized stream
    async def wait(self, h: AgentHandle) -> AgentOutcome: ...   # exit code, claim, usage, failure class
    async def cancel(self, h: AgentHandle, grace_s: float = 10) -> None: ...
    async def resume(self, session_ref: str, req: AgentRequest) -> AgentHandle: ...  # raise Unsupported if not

class AgentRequest(BaseModel):
    attempt_id: AttemptId; workspace: Path
    prompt: str                       # rendered from template (Appendix B); includes context pack
    model: str | None; timeout_s: int
    permissions: AgentPermissions     # translated from policy: allowed tools, write scope, network
    env: dict[str, str]               # already filtered by secrets policy
    session_ref: str | None

class AgentEvent(BaseModel):          # normalized across vendors
    kind: Literal["started","text","tool_call","tool_result","usage","error","finished"]
    ts: datetime; data: dict; raw: dict | None
```

### 10.2 Adapter manifest (`adapters/<id>/manifest.yaml`)
```yaml
id: codex
name: Codex CLI
kind: cli
binary: codex
probe: { version_args: ["--version"], auth_check: "exec_smoke" }
capabilities: { implement: 0.85, debug: 0.8, test: 0.7, review: 0.6, design: 0.5, research: 0.3 }
cost_class: medium
supports: { streaming: true, sessions: true, non_interactive: true, cancel: true, cost_reporting: partial }
```
Capability priors are starting values only; §12 blends them with observed history.

### 10.3 Shared subprocess runner (`agents/subprocess.py`)
- Spawns with `anyio.open_process`, new process group, cwd = workspace, env = filtered env.
- Streams stdout line-by-line to (a) the adapter's parser and (b) the raw capture file.
- Enforces timeout → SIGTERM process group → grace → SIGKILL. Records `FailureClass.TIMEOUT`.
- Captures stderr separately (bounded ring buffer, 256 KB) for error classification.
- No shell=True. Args are lists.

### 10.4 Output contract (critical)
- The control plane, **not the agent**, determines what changed: after `wait()`, the workspace
  module runs `git add -A && git diff --cached --stat/--numstat` against `base_commit` and records a
  `DiffSummary` + full patch artifact.
- Files changed outside `task.file_scope` → `policy.violation` event and `FailureClass.SCOPE_VIOLATION`
  (unless the task is read-only, in which case *any* write is a violation).
- The agent's final message is stored as `claim`. It is shown to reviewers and in reports, labeled
  as a claim. It is never used as a verification signal and never placed in Jev state (§18.6).

### 10.5 Adapter test matrix (MUST, per adapter, using recordings + fake binaries)
availability/probe · successful execution · non-zero exit · malformed stream line · timeout ·
cancellation · auth failure classification · rate-limit classification · usage/cost parsing ·
session resume (or `Unsupported`) · permission-flag translation snapshot.

---

## 11. Adapter implementations

General approach for real CLIs: **discover, don't assume.** CLI flags evolve. Each adapter:
1. Runs `<binary> --version` and `<binary> <subcommand> --help` in `probe()`.
2. Checks the help text for the flags in its manifest's `required_flags`; if absent, health =
   `degraded` with a reason, and an ADR + `docs/adapters/<id>.md` note is written at build time.
3. Ships a parser tested against recorded streams in `tests/fixtures/recordings/<id>/`. When the CLI
   is available and `AIX_LIVE=1`, `aix dev record <id>` captures fresh recordings.

Last-known headless invocations (verify at build time; see Appendix C for details):

| Adapter | Headless invocation (last known) | Stream format | Notes |
|---|---|---|---|
| `claude` | `claude -p <prompt> --output-format stream-json --verbose` (+ permission/tool-allow flags, `--model`, `--resume <id>`) | JSONL events | final `result` event carries usage/cost and session id |
| `codex` | `codex exec --json <prompt>` (+ sandbox/approval flags, `--model`, `-C <dir>`) | JSONL events | resume via `codex exec resume` if present |
| `gemini` | `gemini -p <prompt> --output-format stream-json` (or `json`) (+ `--model`, approval mode) | JSON/JSONL | fall back to `json` if stream unsupported |
| `opencode` | `opencode run <prompt> --format json` (+ `--model`) | JSON events | |
| `ollama` | HTTP `POST /api/chat` on localhost | NDJSON | text-only; capability `research`, `review`, `summarize`; no file edits |
| `fake` | in-process scripted agent (§27.1) | normalized events | deterministic; used by all non-live tests |

Permission translation: each adapter maps `AgentPermissions` to its native flags (allowed tools,
sandbox mode, write roots, approval mode set to non-interactive/"never ask"). The mapping is
snapshot-tested. If a CLI cannot express a restriction, the adapter declares it in
`AgentSpec.supports` and the policy engine decides whether that agent is eligible (§20.2).

---

## 12. Routing engine

Input: `Task`, eligible `AgentSpec`s (healthy, enabled, policy-permitted), `AgentStats`, budget state,
run history (which agents already failed this task), config.

Algorithm (deterministic; `rules` strategy):
```text
eligible = agents where health ∈ {ready, degraded*} and all required capabilities supported
           and policy.allows(agent, task)            (*degraded only if no ready agent)
for each agent:
  prior    = mean(agent.capabilities[c] for c in task.required_capabilities)
  observed = bayesian_success_rate(stats[agent, task.type])     # Beta(2,2) prior; §22.3
  score    = 0.45*observed + 0.35*prior + 0.10*cost_fit + 0.10*latency_fit
  penalties: -0.5 if agent failed this task already (unless mutation=same_agent_new_context)
             -1.0 if task.type in {review, security_review} and agent authored the reviewed change
                   and routing.prefer_independent_reviewer
             -inf if budget remaining < agent expected cost
primary = argmax; fallbacks = next 2 by score
```
Static pins (`routing.static`) override scoring but still pass eligibility and policy.

`jev_assisted` strategy: when the top two scores are within 0.05, ask Jev a `Choice` over the
tied candidates (question `route.pick_agent`, Appendix A) using a compact state (task type,
capabilities, per-agent stats). Accept Jev's pick only if confidence ≥ threshold; otherwise keep the
rules pick. Always record a `DecisionRecord` with point `routing`.

Output: `RoutingDecision{primary, fallbacks, scores, reason_codes}`; emitted as `agent.selected`.
Unit tests: table-driven cases including "no eligible agent" → task `failed` with
`FailureClass.NO_ELIGIBLE_AGENT`.

---

## 13. Intent and planning

### 13.1 Intent
`IntentEngine.parse(goal, repo_facts) -> Intent`. MVP: rules + keyword heuristics for `kind` and
`risk` (e.g. words like deploy/production/migrate/delete/credentials → `high`). Optionally, with Jev
enabled, `intent.classify` asks `Choice(kind)` + `Choice(risk)`; rules are the fallback. Repo facts
come from a cheap local inspection (languages, package managers, test commands found, size).

### 13.2 Planners
- **AgentPlanner** (default when any agent with `design` capability ≥0.6 is ready): renders
  Appendix B.1 prompt with intent, repo facts, available skills and task types, and the TaskGraph
  JSON Schema. Runs the agent **read-only** in a throwaway worktree. Extracts the last JSON object
  from the output, validates against the schema + graph invariants.
  Repair loop: up to 2 re-prompts including the validation errors verbatim. Then fall back.
- **TemplatePlanner** (always available, deterministic): builds a graph from the chosen skill's
  `workflow.yaml` (§16). Default coding template: `inspect → design → implement → test → review →
  security_review → document`, with `test` and `review` parallel after `implement`, and
  `security_review` added only when intent risk ≥ medium or the skill requires it.

### 13.3 Post-processing (both planners)
- Cap at `planner.max_tasks`; merge excess into parents.
- Normalize capabilities to the known vocabulary; unknown → ADR-worthy warning, dropped.
- Add implicit ordering edges for overlapping `file_scope` (§14.3).
- Every write task gets a `verification` spec: skill's spec ∪ `verification.required_default`.
- Persist as `run.planned` with the graph; print it (`aix plan show <run>`).

`aix run --plan-only "<goal>"` stops after planning (useful for tests and for users).

---

## 14. Scheduler, orchestrator, and workspaces

### 14.1 Scheduler
- Maintains the ready set (deps completed). Runs up to `execution.max_parallel` attempts.
- Read-only tasks may share one worktree snapshot; write tasks each get their own worktree.
- Cancellation (`aix cancel <run>` or Ctrl-C): cancel all running attempts (adapter `cancel`),
  mark tasks `cancelled`, run `finalizing` to emit partial artifacts.
- Budget checks before every attempt start and after every attempt: exceeding any budget emits
  `budget.exceeded`, triggers decision point `budget` (§18.2), which may only `stop` or `ask_human`.

### 14.2 Attempt loop (per task)
```text
route → create worktree at integration-branch HEAD → build context pack (§15)
→ render prompt → adapter.start → stream → wait → capture diff → scope check
→ verification (§17) → decision point task_completion (§18)
→ accept: mark accepted, enqueue integration
→ retry/switch_agent/escalate: failure triage (§19) chooses mutation → new attempt
→ ask_human: create approval, pause task
→ reject/stop: fail task, block dependents
```

### 14.3 Workspaces (git worktrees)
- Pre-flight: project must be a git repo with a clean working tree for tracked files, or
  `aix run --allow-dirty` which snapshots the dirty state into a commit on a temp branch.
- Run branch: `aix/run/<run_id>` created from current HEAD. It is the integration branch.
- Attempt worktree: `.aix/worktrees/<attempt_id>` on branch `aix/att/<attempt_id>` from the run
  branch's HEAD at attempt start.
- On accept: commit attempt changes (`aix: <task title> [<task_id>/<attempt_id>]`) and merge into the
  run branch with `--no-ff`. Merges are serialized (a single integration lock).
- Merge conflict → `workspace.conflict` event, `FailureClass.MERGE_CONFLICT` → retry mutation
  `rebase_and_retry` (new attempt from new run-branch HEAD, conflict summary in context).
- Overlapping `file_scope` between unordered write tasks → planner adds an edge (earlier-listed
  first) so conflicts are prevented rather than resolved.
- On run completion the user's current branch is **not** modified. Output: "Results are on branch
  `aix/run/<id>`. Merge with: `git merge aix/run/<id>`". `aix run --apply` fast-forwards/merges into
  the current branch only if clean; this is an explicit user flag (never implicit, §20).
- Worktrees are removed after the run unless `--keep-worktrees`; branches are kept.

---

## 15. Context fabric

### 15.1 Stores
- **Project context** (`project_facts`): languages, frameworks, commands, conventions (read from
  `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, `README` if present), key files. Cached, refreshed on
  HEAD change.
- **Run context**: intent, plan, decisions so far.
- **Task context**: goal, constraints, dependency outputs (handoffs), previous attempts' failure
  summaries.
- **Long-term memory**: deferred beyond MVP (§32), interface stubbed.

### 15.2 Handoffs (Agent A → Agent B without the user pasting anything)
When a task is accepted, the orchestrator builds a `Handoff`:
```json
{ "task_id": "...", "summary": "<generated from diff stat + checks + claim, labeled>",
  "files_changed": ["..."], "decisions": ["dec_..."], "open_issues": ["warning checks"],
  "artifacts": ["art_..."] }
```
`summary` is produced deterministically from facts (diffstat, check results), plus the agent claim
clearly delimited as `AGENT CLAIM (unverified)`. Dependents receive handoffs of their direct deps.

### 15.3 Context pack assembly
Token budget per agent (default 24k tokens of context, configurable). Priority order: task goal &
constraints → dependency handoffs → previous-attempt failure summary → relevant project facts →
file list hints. Truncate from the bottom. Never include: secrets, raw env, other agents' full
transcripts, Jev internals.

### 15.4 Compaction
When a run's decision/handoff log exceeds the budget, compact into
`{summary, decisions, open_questions, known_failures, important_files}` using a `summarize`-capable
agent (or deterministic concatenation when none). Emit `context.compacted` with before/after hashes.

---

## 16. Skills

Layout (`skills/builtin/<name>/`):
```text
skill.yaml         # name, description, task_types, required_capabilities, inputs
instructions.md    # appended to the agent prompt for tasks using this skill
workflow.yaml      # task template for TemplatePlanner
verification.yaml  # required/optional checks and thresholds
```
MVP builtin skills: `inspect-repo`, `feature-implementation`, `bugfix`, `code-review`,
`security-review`, `write-tests`, `documentation`. Skills never name a vendor. Skill loading
validates all four files against schemas; `aix skill list|inspect` works.

`code-review` and `security-review` MUST produce structured findings: the prompt requires a final
JSON block `{"findings":[{"severity","file","line","title","detail","confidence"}]}` which the
`ai_review` check parses (§17.3). Unparseable → check status `error`.

---

## 17. Verification engine

### 17.1 Toolchain detection (`verification/detect.py`)
| Signal | build | tests | lint | typecheck |
|---|---|---|---|---|
| `pyproject.toml` / `setup.cfg` | `python -m compileall -q src` | `pytest -q` (if configured) | `ruff check` if installed | `mypy`/`pyright` if configured |
| `package.json` | `scripts.build` | `scripts.test` | `scripts.lint` | `scripts.typecheck` or `tsc --noEmit` if tsconfig |
| `go.mod` | `go build ./...` | `go test ./...` | `go vet ./...` | — |
| `Cargo.toml` | `cargo build` | `cargo test` | `cargo clippy` | — |
| `Makefile` targets | `build` | `test` | `lint` | — |
Explicit `verification.commands` always win. Detected commands are recorded in the report.

### 17.2 Check runner
- Runs in the attempt worktree, with the security policy's shell allowlist and `network: deny` for
  checks by default (container sandbox enforces; local mode best-effort, §20.3).
- Captures stdout/stderr to artifacts, parses structured output where possible: JUnit XML
  (`pytest --junitxml`), SARIF (semgrep), JSON (gitleaks, pip-audit, npm audit).
- Metrics extracted: `tests_total/passed/failed/skipped`, `lint_errors`, `type_errors`,
  `findings_{critical,high,medium,low}`.
- A required check whose tool is missing → `skipped` with summary "tool not available" → report
  `incomplete` (not `passed`). This is intentional: missing verification is not success.

### 17.3 Check kinds
- `build`, `tests`, `lint`, `typecheck`: command-based.
- `security_sast`: semgrep (`--config auto` offline rules if available) / bandit for Python.
- `secrets`: gitleaks on the diff; any finding → `critical`, failed.
- `deps`: pip-audit / npm audit; high/critical → failed; others warning.
- `ai_review`: an **independent** agent (router penalty §12) reviews the diff with the
  `code-review` skill; findings parsed; any `high`/`critical` finding with confidence ≥0.6 → failed.
  AI review is evidence *about* the diff, recorded with the reviewer identity; it never overrides a
  failed executed check.
- `policy`: scope violation, forbidden file types (e.g. committed `.env`), binary blobs > size limit.
- `custom`: from skill `verification.yaml` (command + pass criteria).

### 17.4 Baseline comparison
Before the first write attempt in a run, run the task's checks on the untouched run branch to get a
**baseline**. A check that failed at baseline and still fails is recorded as `pre_existing` and does
not fail the attempt unless the failure count increased. This prevents punishing agents for a
repo's existing red tests. Baseline results are an artifact.

---

## 18. Decision Service and Jev

### 18.1 Principles
1. **Policy is authority; Jev is judgment.** Hard gates run in code first and cannot be overridden.
2. **Ask narrow, typed questions over compact, factual state.** No free-text "is this good?".
3. **Thresholds live in config, per decision point**, scaled to the cost of being wrong.
4. **Every decision is recorded** with exact state, questions, answers, confidence, distribution,
   provider/model version, and `inputs_hash` so it can be replayed.
5. **Jev failure never blocks a run**: timeout/error → `rules` provider, with reason code
   `jev:unavailable`.

### 18.2 Decision points
| Point | When | Allowed outcomes |
|---|---|---|
| `task_completion` | after verification of an attempt | accept, retry, switch_agent, escalate, ask_human, reject |
| `failure_triage` | after a failed attempt | chooses `FailureClass` refinement + `RetryMutation` (§19) |
| `routing` | tie-break only (§12) | choose:<agent> |
| `tool_risk` | before a gated tool call or high-risk command is executed by the control plane | allow, deny, ask_human |
| `plan_review` | after planning, when intent risk = high | accept, ask_human, reject |
| `budget` | when a budget is exceeded | stop, ask_human |
| `run_completion` | all tasks terminal | accept (run completed) or reject (run failed) |

### 18.3 Hard gates (`decision/gates.py`) — evaluated before any provider
- Any required check `failed`/`error` → outcome cannot be `accept`.
- Report `incomplete` → cannot be `accept` (may be `ask_human` or `retry` with `more_verification`).
- `secrets` finding or `policy` violation → `reject` for this attempt (retry allowed with mutation).
- Attempt count ≥ `task.max_attempts` → only `escalate` (if an escalation step remains), `ask_human`,
  or `reject`.
- Action in `security.approval_required_for` → `ask_human` regardless of provider.
- Run budget exhausted → `stop` or `ask_human` only.
The gate returns `GateResult{forced_outcome | allowed_outcomes, reason_codes}`. Providers can only
pick among allowed outcomes.

### 18.4 Providers
```python
class DecisionProvider(Protocol):
    name: str
    async def decide(self, point: DecisionPoint, state: DecisionState,
                     allowed: set[DecisionOutcome]) -> ProviderAnswer: ...
```
- **`rules`** (default): deterministic table per point. E.g. `task_completion`: overall `passed` →
  accept; `warning` with only low/info → accept; `warning` with medium → retry once then accept with
  note; `failed` → retry (mutation from triage).
- **`jev`**: builds the typed question set for the point (Appendix A), calls Jev, maps answers to
  outcomes with thresholds (§18.5). Wrapped behind:
  ```python
  class JevClient(Protocol):
      async def system_one(self, state: dict | str, questions: dict[str, JevQuestion]) -> JevResponse: ...
  ```
  Real implementation uses the `typesafe-sdk` package (`TypeSafeClient().system_one(state=...,
  questions={...})` with `Choice`, `Score`, `Noul`; answers expose `.choice`, `.score`, `.noul`,
  `.confidence` and distributions; key from `TYPESAFE_API_KEY`). If the SDK is sync, call via
  `anyio.to_thread.run_sync`. Verify the SDK surface at build time against installed package
  introspection and TypeSafe docs; record the verified signature in an ADR.
- **`fake`**: `FakeJevClient` returns scripted answers keyed by question name; used by all tests.
- **`human`**: used when the outcome is `ask_human` and a human resolves it via `aix approve/deny`.

### 18.5 Mapping Jev answers to outcomes (`task_completion` example)
Questions (see Appendix A.1): `completion` (Choice: accept / fix_and_retry / different_agent /
needs_human), `residual_risk` (Score, 4 levels), `warnings_blocking` (Noul).
```text
if gate forced → forced outcome
a = answers
if a.completion.choice == "accept"
   and a.completion.confidence >= thresholds.task_completion.accept_confidence   # default 0.85
   and a.warnings_blocking.noul < 0.3
   and a.residual_risk.score < 1.5            → accept
elif a.completion.confidence < 0.5            → rules provider decides (reason: jev:low_confidence)
elif choice == "fix_and_retry"                → retry
elif choice == "different_agent"              → switch_agent
elif choice == "needs_human"                  → ask_human
else                                          → rules provider decides
```
Thresholds are per point and per risk: for `task.risk == high`, accept confidence default 0.92.
Record both the Jev answer and the final outcome; they may differ, and `reason_codes` say why.

### 18.6 Building Jev state (security-critical)
Jev does not treat state as hostile, so text engineered inside state can move its answer. Therefore
state is built **only from control-plane facts**, never from agent prose or repository content:
```json
{
  "task": {"type":"implement","risk":"medium","attempt":2,"max_attempts":3},
  "verification": {"overall":"warning",
     "checks":[{"kind":"tests","status":"passed","tests_total":42,"tests_failed":0},
               {"kind":"security_sast","status":"warning","findings_high":0,"findings_medium":2}]},
  "diff": {"files_changed":5,"lines_added":180,"lines_removed":12,"touches_scope_only":true},
  "history": {"previous_failures":["verification_failure:tests"],"agent_switched":false},
  "review": {"reviewer_independent":true,"findings_high":0,"findings_medium":1}
}
```
Rules: numbers, enums, and control-plane-generated short labels only. No agent claims, no code, no
file contents, no commit messages, no finding descriptions (only counts/severities). Keep state
minimal — Jev accuracy degrades with irrelevant material. A unit test asserts that `DecisionState`
builders reject free-text fields longer than 64 chars and any field sourced from `ExecutionResult.claim`.

### 18.7 Evaluation harness (MUST before enabling `jev` by default anywhere)
`tests/decision/eval_cases/*.yaml`: labeled `(state, expected_outcome)` cases (≥40 for
`task_completion`, ≥20 for `failure_triage`), including adversarial cases. `aix dev eval-decisions
--provider rules|jev` reports accuracy, confusion matrix, and calibration (reliability bins).
`rules` must hit 100% on its own table cases. `jev` live eval runs only with `AIX_LIVE=1`; results
are saved as an artifact and summarized in an ADR. Default provider stays `rules` until a live eval
shows Jev ≥ rules accuracy on the suite; this switch is an ADR, not a silent config change.

---

## 19. Failures, retries, escalation

### 19.1 Failure taxonomy (`FailureClass`)
`AGENT_FAILURE` (non-zero exit, crash) · `AGENT_NO_CHANGES` (write task, empty diff) ·
`TOOL_FAILURE` · `NETWORK_FAILURE` · `AUTH_FAILURE` · `RATE_LIMITED` · `TIMEOUT` ·
`VERIFICATION_FAILURE` (+ sub-kind: tests|build|lint|typecheck|security|review) ·
`SCOPE_VIOLATION` · `POLICY_FAILURE` · `MERGE_CONFLICT` · `CONTEXT_FAILURE` (prompt too large,
missing dependency output) · `RESOURCE_FAILURE` (disk, memory) · `BUDGET_EXCEEDED` ·
`NO_ELIGIBLE_AGENT` · `INTERRUPTED` · `HUMAN_REJECTION`.
Classification: adapter-specific stderr/stream patterns first (tested with recordings), then
generic rules. With Jev enabled, `failure_triage` may refine *among classes consistent with the
evidence* (Choice restricted to candidates the rules produced).

### 19.2 Retry mutations (`RetryMutation`)
| Failure | Default mutation sequence |
|---|---|
| VERIFICATION_FAILURE:tests/build/typecheck | 1) `same_agent_with_failure_context` (failing output excerpt, ≤4k tokens) 2) `switch_agent` 3) `add_research_step` then implement 4) `ask_human` |
| VERIFICATION_FAILURE:security/review | 1) `same_agent_with_findings` 2) `switch_agent` 3) `ask_human` |
| AGENT_NO_CHANGES | 1) `same_agent_clarified_prompt` 2) `switch_agent` |
| TIMEOUT | 1) `split_task` (planner splits into ≤3 subtasks) 2) `switch_agent` |
| RATE_LIMITED / NETWORK_FAILURE | `wait_and_retry` (exp. backoff 30s,120s) then `switch_agent`; does not consume an attempt |
| AUTH_FAILURE | mark agent `unavailable` for the run; `switch_agent`; does not consume an attempt |
| MERGE_CONFLICT | `rebase_and_retry` |
| SCOPE_VIOLATION | `same_agent_with_scope_reminder`, then `switch_agent` |
| CONTEXT_FAILURE | `compact_context_and_retry` |
| POLICY_FAILURE, BUDGET_EXCEEDED, HUMAN_REJECTION | no automatic retry; `ask_human` or fail |
Every retry records `mutation` + `reason_codes` (Rule: every retry has a reason). Identical
(agent, prompt-hash, base-commit) retries are forbidden — the orchestrator asserts this.

### 19.3 Escalation ladder (policy-driven, configurable)
`default model → stronger model (same agent, if model_select) → different agent → multi-agent
(implementer + independent reviewer pair, reviewer findings fed back) → human`.
Hard limits: `task.max_attempts` (default 3, max 6), `budget.max_attempts_per_run`.

---

## 20. Security and policy

### 20.1 Threat model (honest)
Agents are powerful local processes running as the user. aix reduces blast radius and makes actions
visible; in `local` mode it cannot fully contain a malicious agent. Assets: user's repo, other files,
credentials, remote systems. Threats: prompt injection from repo content or web, agents writing
outside scope, exfiltration via network, agents self-approving gated actions, secrets leaking into
logs/context/artifacts, decision-provider manipulation via crafted state.

### 20.2 Policy engine (`security/policy.py`)
Declarative, versioned (policy hash is part of every DecisionRecord). Evaluates:
`can_run_agent(agent, task)`, `agent_permissions(task) -> AgentPermissions`,
`can_exec(cmd, context)`, `requires_approval(action)`, `can_write(path, task)`.
Agents whose CLI cannot enforce the task's required restriction (e.g. write-scope) are ineligible
for `high`-risk tasks in `local` mode.

### 20.3 Sandbox modes
- `local`: worktree isolation + agent-native sandbox flags + scope check after execution. Network
  restriction for checks is best-effort (documented).
- `container` (M8): attempts run inside a container (docker/podman) with only the worktree mounted
  read-write, no home directory, egress restricted to the agent provider endpoints via an allowlist
  proxy. Required for `security.sandbox: container` and recommended for untrusted repos.
  If no container runtime is available, runs requiring `container` fail with a clear message.

### 20.4 Approvals and integrity
- Gated actions (`approval_required_for`) create a pending `Approval`; the task waits.
- `aix approve <approval_id|task_id>` and `aix deny ...` require an interactive TTY confirmation
  (typing the short id back). Non-TTY approval requires `--token` matching a token stored in the
  user config dir (`~/.config/aix/approval_token`, mode 0600), which is never placed in agent env
  or context.
- Every agent subprocess gets `AIX_AGENT_CONTEXT=1`; `aix approve` refuses when it is set.
- In `local` mode a same-user process could still read the token file; this residual risk is
  documented in `docs/security.md`. In `container` mode the token is not mounted.
- Agents never receive the aix DB path or credentials for aix itself.
- No irreversible action is implicit: pushing, deploying, applying to the user's branch
  (`--apply`), deleting files outside scope — all require an explicit flag or approval.

### 20.5 Secrets
- Provider credentials are read from env/keychain at adapter start and injected only into that
  adapter's process env (per-adapter allowlist of env var names in the manifest).
- Agent env = minimal base (PATH, HOME or container HOME, LANG, TERM) + adapter allowlist. All other
  env vars are dropped.
- Redaction: a redactor with known-secret values (from env at startup) and regexes (API-key shapes)
  is applied to logs, stream captures, events, artifacts, and prompts. Test: a fake key placed in
  env never appears in any written file after a fake run.

---

## 21. Artifacts and provenance

### 21.1 Store
Content-addressed blobs under `.aix/artifacts/objects/`; metadata in the `artifacts` projection.
Writing the same content twice yields the same `sha256` and one blob.

### 21.2 Provenance (every artifact)
```json
{ "run_id":"...", "task_id":"...", "attempt_id":"...",
  "producer": {"kind":"agent|check|control_plane|reviewer", "id":"codex", "model":"...", "version":"..."},
  "tools": ["pytest 8.x"], "inputs": ["art_..."], "base_commit":"...", "result_commit":"...",
  "decisions": ["dec_..."], "policy_hash":"...", "aix_version":"...", "created_at":"..." }
```

### 21.3 Standard artifacts per run
`plan.json` · `patch/<task>.diff` · `verification/<attempt>.json` + raw logs/junit/sarif ·
`decision-log.json` (all DecisionRecords) · `agent-trace.json` (attempts, agents, models, durations,
usage) · `report.md` + `report.html` (human summary, §23.3 layout) · `manifest.json` (all
artifacts with hashes; itself hashed). `aix artifact export <run> --bundle` zips them with the
manifest; `aix artifact verify <bundle>` re-hashes and checks the manifest.

### 21.4 Determinism
Report rendering is deterministic given the same events (sorted keys, stable ordering, no wall-clock
in content except recorded timestamps). Snapshot tests cover `report.md` and `decision-log.json`.

---

## 22. Observability and cost

- **Tracing:** each run is a trace; tasks/attempts/checks/decisions are spans derived from events.
  `aix trace <run>` renders a tree with durations and outcomes. Optional OTLP exporter (post-MVP).
- **Cost:** from adapter usage (tokens, `cost_usd` if the CLI reports it; otherwise estimated from a
  price table in config and marked `estimated: true`). Budget enforcement uses reported-or-estimated.
- **Agent stats** (`agent_stats` projection): per (agent, model, task_type): attempts, accepted,
  verification pass rate, mean cost, p50/p90 latency, retry rate, human-intervention rate. Feeds
  routing (§12). `aix stats agents` prints it. No global "best model" ranking is displayed.
- **Telemetry** is local only. Nothing is sent anywhere except to the configured agent providers
  and Jev.

---

## 23. CLI specification

### 23.1 Commands (MVP)
```text
aix init [--force]                      create .aix/, config, gitignore entries; detect toolchain
aix doctor                              check git, python, agents (probe), container runtime, jev key, security tools
aix config show [--resolved]
aix agent list | inspect <id> | test <id> | enable <id> | disable <id>
aix skill list | inspect <id>
aix run "<goal>" [--plan-only] [--agent <id>] [--skill <id>] [--max-parallel N]
                 [--budget-usd X] [--decision-provider rules|jev] [--allow-dirty]
                 [--apply] [--keep-worktrees] [--json] [--resume <run_id>]
aix plan show <run>
aix status [<run>]                      live table of tasks (refreshes while running)
aix logs <run> [--task <id>] [--follow]
aix trace <run>
aix verify [--path .]                   run verification on current tree (no agents)
aix review [--agent <id>] [--base <ref>]   independent AI review of current diff
aix approvals | approve <id> | deny <id> [--reason]
aix cancel <run>
aix artifact list <run> | show <id> | export <run> --bundle | verify <bundle>
aix stats agents
aix dev export-schemas | rebuild-projections | record <agent> | eval-decisions
```
Aliases: `aix agents` = `aix agent list`.

### 23.2 Output and exit codes
- Human output via `rich`; `--json` emits machine-readable JSON (one object, or JSONL for `--follow`).
- Exit codes: `0` success · `1` run failed · `2` usage/config error · `3` waiting for approval ·
  `4` cancelled · `5` environment problem (doctor-class) · `6` budget exceeded.

### 23.3 Final run summary (stdout and `report.md`)
```text
RUN run_01J… COMPLETED                  branch: aix/run/run_01J…
Goal: Add JWT authentication to this repository

Tasks        7/7 completed   (1 retry, 0 human interventions)
Tests        42/42 passed    (baseline: 40/40)
Security     no blocking findings (2 low)
Review       independent (opencode): 0 high, 1 medium (addressed)
Decisions    9 recorded  (rules: 7, jev: 2)   final: accepted
Agents       claude (design), codex (implement ×2), opencode (review)
Cost         $1.84 (reported $1.61 + estimated $0.23)   Duration 14m12s

Artifacts    .aix/artifacts/bundles/run_01J….zip
  plan.json · report.html · decision-log.json · agent-trace.json · verification/*.json · patch/*.diff

Next: git merge aix/run/run_01J…
```

### 23.4 Startup performance
`aix --help` < 300 ms; `aix` startup < 500 ms excluding provider probes (lazy-import heavy modules;
test with a timing check that is skipped on slow CI via env flag).

---

## 24. API (M9)

FastAPI app in `aix.api`, same services as the CLI (no duplicate orchestration). Endpoints:
`POST /runs`, `GET /runs/{id}`, `GET /runs/{id}/events` (SSE), `GET /runs/{id}/tasks`,
`POST /runs/{id}/cancel`, `GET /approvals`, `POST /approvals/{id}` (requires API token with
`approve` scope), `GET /artifacts/{id}`. Binds to `127.0.0.1` by default. `aix serve`.
A contract test runs the same golden scenario through CLI and API and compares resulting events.

## 25. Plugins

Entry-point groups: `aix.adapters`, `aix.tools`, `aix.skills`, `aix.checks`, `aix.decision_providers`,
`aix.exporters`. Plugin manifest fields: `id, version (semver), type, capabilities, permissions,
requires_aix (semver range), entrypoint`. Loading validates the manifest and version range; a
plugin failing to load is reported by `aix doctor` and skipped, never crashing the CLI. Builtin
adapters are registered through the same mechanism (dogfooding the contract).

---

## 26. Testing strategy

| Layer | What | Where |
|---|---|---|
| Unit | domain models, state machines (hypothesis), router scoring, gates, rules provider, Jev mapping, context packing, redaction, config merge, detection | `tests/unit` |
| Adapter | §10.5 matrix per adapter with recordings and fake binaries (small Python scripts placed on PATH that emit recorded streams) | `tests/adapter` |
| Integration | planner→router→agent(fake)→verifier→decision on the fixture repo; event replay; resume after kill | `tests/integration` |
| Golden | §28 scenarios end-to-end with fake agents; snapshot reports | `tests/golden` |
| Live | real CLIs and Jev; `AIX_LIVE=1` only; tiny prompts on a scratch copy of the fixture repo | `tests/live` |
| Contract | CLI vs API equivalence; schema drift; import layers | `tests/contract` |

Coverage target: ≥85% lines on `domain`, `core`, `decision`, `security`; enforced in `make check`
from M3 onward. No network in non-live tests (enforced by a pytest plugin that blocks sockets
except localhost for the API tests).

## 27. Fakes and fixtures

### 27.1 Fake agent
`adapters/fake/` implements the full protocol in-process. Behavior is scripted per test by YAML:
```yaml
# tests/fixtures/agent_scripts/auth_retry.yaml
match: { task_type: implement }
attempts:
  - events: [{kind: text, data: {text: "Implementing JWT"}}]
    apply_patch: patches/auth_v1_broken.diff     # applied to workspace
    claim: "Done. All tests pass."               # deliberately false
    exit_code: 0
  - apply_patch: patches/auth_v2_fixed.diff
    claim: "Fixed failing test."
    exit_code: 0
```
Also supports: `sleep_s` (timeouts), `exit_code != 0`, `stderr` (classification), `write_outside_scope`,
`usage`, `emit_findings` (for review tasks), `planner_output` (for AgentPlanner tests).
Several fake agents with different ids/capabilities can be registered (`fake-a`, `fake-b`, `fake-reviewer`)
to exercise routing and independence.

### 27.2 Fixture repository `tests/fixtures/repos/sample_py`
A tiny Python web-free app (`app/` with a user store and a request handler function), `pyproject.toml`,
pytest tests (all passing at baseline), ruff config. Tests copy it to a temp dir and `git init` it.
Patches in `tests/fixtures/agent_scripts/patches/` implement: hello endpoint, JWT auth (broken and
fixed variants), a change with a planted fake secret, a change outside scope, a review-findings set.

## 28. Golden scenarios (MUST pass in `make golden`)

| ID | Goal | Setup | Expected (asserted on events + report) |
|---|---|---|---|
| G1 simple | "Add a hello endpoint" | 1 fake agent | 1–3 tasks; one write attempt; tests pass; decision accept; artifacts incl. manifest |
| G2 multi-agent | "Add JWT authentication" | fake-a (design), fake-b (implement), fake-reviewer | ≥5 tasks; ≥2 distinct agents; review by agent ≠ implementer; handoff from design visible in implement prompt capture |
| G3 failure→retry | same, with `auth_retry.yaml` | | attempt 1 tests fail despite claim "All tests pass"; failure `VERIFICATION_FAILURE:tests`; mutation `same_agent_with_failure_context`; attempt 2 passes; final accept; claim labeled unverified in report |
| G4 high-risk | "Deploy to production" / patch touching `deploy/` | | intent risk high; `plan_review` → ask_human; exit code 3; `aix approve` (TTY simulated) resumes; `aix deny` fails the run |
| G5 unavailable | primary agent `health=unavailable` | | router picks fallback; `agent.selected` reason includes fallback |
| G6 scope violation | agent writes outside `file_scope` | | `policy.violation`; attempt rejected; retry with scope reminder |
| G7 secret leak | patch contains fake AWS-shaped key | | `secrets` check critical → reject; key never appears in logs/artifacts except redacted |
| G8 Jev provider | `--decision-provider jev` with FakeJevClient | scripted answers incl. low confidence | low confidence → rules fallback recorded; high confidence accept only when gates allow; gate beats Jev when Jev says accept but a required check failed |
| G9 crash resume | kill orchestrator mid-attempt | | `aix run --resume` marks INTERRUPTED, retries, completes |
| G10 budget | budget $0.01, fake usage $0.05 | | `budget.exceeded`; outcome stop; exit 6 |

## 29. Performance targets (MVP)
CLI startup < 500 ms (excl. probes) · task creation < 100 ms · routing < 1 s excl. model calls ·
event write p99 < 20 ms · report generation < 2 s for 100 tasks · Jev call timeout 3 s with fallback ·
overhead per attempt (excluding agent + checks) < 3 s.

---

## 30. Milestones and exit gates

Each milestone: implement listed sections → all PROGRESS items ticked → **Exit Gate commands pass**
→ `git tag m<N>-done`. Gate commands run in a fresh shell from repo root.

### M0 — Scaffold (§4, §5, §26 skeleton)
Deliver: pyproject, uv lock, Makefile, ruff/pyright/import-linter configs, empty package tree,
pytest config with socket blocker and `live` marker, CI-equivalent `make check`, docs skeleton.
Gate:
```bash
make check && uv run aix --help && uv run lint-imports
```

### M1 — Domain, store, config (§6–§9)
Deliver: all domain models + JSON schema export, state machines with hypothesis tests, SQLite event
store + migrations + projections + rebuild, layered config + `aix config show --resolved`, `aix init`.
Gate:
```bash
make check
uv run aix dev export-schemas && git diff --exit-code schemas/
uv run pytest tests/unit/domain tests/unit/store -q
cd "$(mktemp -d)" && git init -q && uv run --project "$OLDPWD" aix init && test -f .aix/config.yaml
```

### M2 — Adapter protocol, registry, fake + first real adapter, basic run (§10, §11, §23 partial)
Deliver: protocol, subprocess runner, registry, `fake` adapter, `claude` adapter (recordings-based
parser), `aix agent list|inspect|test`, `aix doctor`, `aix run "<goal>" --agent <id>` executing a
**single task** in a worktree with diff capture and events. Also `codex` adapter.
Gate:
```bash
make check
uv run pytest tests/adapter -q
uv run pytest tests/integration/test_single_task_run.py -q   # fake agent end-to-end: run → diff → events
uv run aix agent list --json | python -c "import json,sys; ids=[a['id'] for a in json.load(sys.stdin)]; assert {'claude','codex','fake'}<=set(ids), ids"   # listed even when unavailable (health shown)
```

### M3 — Planning, routing, scheduling, workspaces (§12–§14, §16)
Deliver: intent engine, AgentPlanner + TemplatePlanner, graph invariants, router (rules strategy),
scheduler with parallelism and dependencies, worktree merge flow + conflict handling, builtin
skills, `gemini` + `opencode` adapters, `aix plan show`, `aix status`, `--plan-only`.
Gate:
```bash
make check
uv run pytest tests/integration/test_multi_task_run.py tests/integration/test_merge_conflict.py -q
uv run pytest tests/golden -k "G2 or G5" -q
```

### M4 — Verification engine (§17)
Deliver: toolchain detection, check runner, parsers (junit, sarif, json), baseline comparison,
`secrets`/`deps`/`sast` checks when tools exist, `ai_review` check with independent reviewer,
`aix verify`, `aix review`.
Gate:
```bash
make check
uv run pytest tests/unit/verification tests/integration/test_verification.py -q
uv run pytest tests/golden -k "G1 or G6 or G7" -q
```

### M5 — Decision service, gates, rules + Jev providers, retries, escalation (§18, §19)
Deliver: DecisionService, gates, rules provider, FakeJevClient, JevDecisionProvider with state
builders + question catalog, eval harness + labeled cases, failure classification, mutation
sequences, escalation ladder, approvals (`aix approvals|approve|deny`), budget decisions.
Gate:
```bash
make check
uv run aix dev eval-decisions --provider rules --fail-under 1.0
uv run pytest tests/unit/decision tests/integration/test_retry_escalation.py -q
uv run pytest tests/golden -k "G3 or G4 or G8 or G10" -q
```

### M6 — Context fabric (§15)
Deliver: project facts, handoffs, context pack budgeting, compaction, prompt capture artifacts.
Gate:
```bash
make check
uv run pytest tests/unit/context tests/integration/test_handoff.py -q   # agent B prompt contains agent A handoff
```

### M7 — Artifacts, provenance, reports, observability (§21, §22)
Deliver: artifact store, provenance, standard artifacts, md/html reports, bundle export/verify,
`aix trace`, `aix logs`, `aix stats agents`, cost accounting, resume after crash.
Gate:
```bash
make check
uv run pytest tests/golden -q                     # ALL of G1–G10
uv run pytest tests/integration/test_bundle_verify.py tests/integration/test_resume.py -q
```

### M8 — Security hardening (§20)
Deliver: policy engine with versioned hash, env filtering + redaction everywhere, approval
integrity (TTY/token, AIX_AGENT_CONTEXT guard), container sandbox mode (skip tests if no runtime),
`docs/security.md` with the threat model and residual risks, `ollama` adapter.
Gate:
```bash
make check
uv run pytest tests/security -q
uv run pytest tests/security/test_prompt_cannot_bypass_policy.py -q   # agent attempts `aix approve` / writes to approval token path → denied & recorded
```

### M9 — API (§24) and plugins (§25)
Deliver: FastAPI service with SSE, token scopes, `aix serve`, CLI/API equivalence test, plugin
loading via entry points (builtins migrated), sample external plugin in `tests/fixtures/plugins/`.
Gate:
```bash
make check
uv run pytest tests/contract -q
```

### M10 — Live validation and final report (§31)
Deliver: live tests for each installed CLI and Jev (skipped cleanly otherwise), `aix dev record`
refreshed recordings where possible, live Jev decision eval if key present (ADR with results),
`FINAL_REPORT.md`.
Gate:
```bash
make check && make golden
test -f docs/playbook/FINAL_REPORT.md
AIX_LIVE=1 make test-live || echo "live tests skipped/failed: documented in FINAL_REPORT.md"
```
(M10's live step is informative: the build is complete if the non-live gate passes and live results,
including skips, are documented.)

Dashboard (web UI) is **out of scope** for this build (§32).

---

## 31. Definition of Done

1. All milestones M0–M10 tagged; `make check` and `make golden` green on a fresh clone.
2. On the fixture repo, with fake agents:
   `aix run "Add JWT authentication to this repository"` →
   inspects repo → plans ≥5 tasks → routes by capability to ≥2 agents → executes in worktrees →
   shares context via handoffs → runs tests + security checks with baseline → independent review →
   Decision Service (gates + provider) records every decision → retries when verification fails →
   integrates onto `aix/run/<id>` → emits the §23.3 summary and a verifiable bundle → complete trace.
3. The same command works against real agents when installed (documented in FINAL_REPORT, with the
   recorded run bundle attached if live tests ran).
4. No policy-gated action can be completed without an approval recorded through a human channel.
5. Every decision in `decision-log.json` can be replayed: `aix dev eval-decisions --replay <bundle>`
   reproduces rules-provider outcomes exactly and reports Jev answer drift separately.

---

## 32. Deferred / out of scope for this build
Web dashboard · mobile · IDE extensions · marketplace · swarm mode (`aix swarm`) · remote/cloud
workers · long-term cross-project memory · learned routing model (beyond Bayesian stats) ·
enterprise auth/billing · automatic production deployment · MCP tool hosting for agents (agents keep
their own MCP configs; aix only records tool calls it sees in streams).
Interfaces for workers, memory, and MCP are stubbed with `NotImplementedError` + ADR so later work
does not require core changes.

---

## Appendix A — Jev question catalog (`decision/questions/`)

Each entry is a Python builder returning `{name: Choice|Score|Noul}` plus the mapping function.
Instructions are literal and narrow (Jev answers exactly what is written).

**A.1 `task_completion`**
- `completion` — Choice. Instructions: "Given only the verification facts, what should happen to this attempt". Criteria:
  `accept`: "All required checks passed and no unresolved medium or higher findings";
  `fix_and_retry`: "A specific check or finding failed that the same agent can address";
  `different_agent`: "Repeated failures of the same kind suggest a different agent should try";
  `needs_human`: "Facts are contradictory, incomplete, or the risk is high".
- `residual_risk` — Score, levels: "No remaining findings" / "Only low findings" / "Medium findings remain" / "High or critical findings remain".
- `warnings_blocking` — Noul: "At least one warning in the verification facts should block acceptance for a task of this risk level".

**A.2 `failure_triage`**
- `failure_class` — Choice restricted to rule-produced candidates.
- `mutation` — Choice over the remaining mutations in §19.2's sequence for that class.
- `likely_transient` — Noul: "The failure facts indicate a transient environmental problem rather than a defect in the change".

**A.3 `tool_risk`** (state: command argv tokens classified by the control plane into enums, target paths relative to workspace, task risk; *not* raw agent text)
- `risk` — Score: "Read-only" / "Local reversible write" / "Local irreversible or wide write" / "External side effect".
- `outside_scope` — Noul: "The target paths are outside the task scope".
Mapping: `risk.score ≥ 2.5` or `outside_scope.noul > thresholds.tool_risk.deny_if_p_risky_above` → deny/ask_human.

**A.4 `route.pick_agent`** — Choice over tied candidate ids, criteria text generated from each agent's stats line.

**A.5 `plan_review`** — Noul: "The plan includes a step with external side effects"; Noul: "The plan omits a verification step for a write task"; Score: plan risk (3 levels).

**A.6 `intent.classify`** — Choice(kind), Choice(risk). State is the goal text — this is the one place user text enters Jev state; it is the user's own input, so injection risk is self-directed, and gates still apply.

## Appendix B — Prompt templates (`core/prompts/*.j2`)

**B.1 Planner** — sections: ROLE (planner producing JSON only) · GOAL · INTENT · REPO FACTS ·
AVAILABLE TASK TYPES + CAPABILITIES (enumerated vocabulary) · AVAILABLE SKILLS · RULES (acyclic,
≤max_tasks, file_scope globs for write tasks, verification per write task, no vendor names) ·
OUTPUT: a single JSON object matching the embedded schema, nothing after it.

**B.2 Task execution** — ROLE · TASK GOAL · CONSTRAINTS · FILE SCOPE (must not modify other files) ·
DEPENDENCY HANDOFFS · PREVIOUS ATTEMPT FAILURES (if any, facts only) · SKILL INSTRUCTIONS ·
DEFINITION OF DONE (checks that will be run; "your statements about success are not accepted as
evidence; the checks are") · DO NOT: commit, push, change git config, touch `.aix/`, read files
outside the workspace.

**B.3 Review** — ROLE (independent reviewer) · DIFF · TASK GOAL · CHECK RESULTS (facts) · OUTPUT:
prose allowed, then a final fenced JSON block `{"findings":[...]}` exactly matching the schema.

All templates are snapshot-tested with fixed inputs.

## Appendix C — Adapter invocation reference (verify at build time)

For each real adapter, `docs/adapters/<id>.md` MUST record: binary version tested, exact argv used,
permission flags mapping, stream event types observed and how each maps to `AgentEvent`, usage/cost
fields, session/resume mechanism, known error strings → FailureClass, and date verified.
Starting points (last known; confirm with `--help`):
- Claude Code: `-p/--print`, `--output-format stream-json` (requires `--verbose`), `--model`,
  `--resume <session_id>`, `--allowedTools`/`--disallowedTools`, `--permission-mode`, `--add-dir`.
- Codex CLI: `exec` subcommand, `--json` event stream, `--model`, `--sandbox` (e.g. `workspace-write`
  / `read-only`), non-interactive approval policy flag, `-C/--cd`.
- Gemini CLI: `-p/--prompt`, `--output-format json|stream-json`, `--model`, approval-mode / yolo flags.
- OpenCode: `run` subcommand, `--format json`, `--model provider/model`.
If a flag is missing, implement the closest supported behavior, set health `degraded` with reason,
and write an ADR.
