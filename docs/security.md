# aix security model

This document says what aix protects, how, and what it does **not** protect. It describes the code
as it is; where a control is partial the text says so. Design background: PLAYBOOK §20 and ADRs
0027-0029.

## 1. Threat model

Agents (Claude Code, Codex, Gemini, OpenCode, local models) are powerful processes that run as the
user. aix reduces their blast radius and makes their actions visible and attributable. **In `local`
mode aix cannot contain a malicious agent**: it runs as your user and can read your files. `container`
mode narrows that considerably (section 4) but has its own gaps.

| Asset | Threats |
|---|---|
| Your repository | agent writes outside its task scope; edits `.aix/` or `.git/`; commits or pushes |
| Other files and credentials | an agent reads `~/.ssh`, env vars, tokens; exfiltrates them over the network |
| aix's own integrity | an agent approves its own gated action; tampers with the event store or artifacts |
| Logs, events, artifacts | secrets leaking into anything aix writes |
| Decisions | crafted text steering the decision provider (prompt injection) |
| Remote systems | irreversible actions: push, deploy, migrations |

Prompt injection is assumed: repository content, web content and other agents' output can carry
instructions. aix therefore never lets agent prose decide anything (section 6).

## 2. Controls that exist today

**Isolation.** Every attempt runs in its own git worktree on its own branch; results reach the run
branch only through serialized merges (`aix/run/<id>`), never your checked-out branch.

**Scope check.** After each attempt the control plane computes the diff itself and compares every
changed path with the task's `file_scope`. `.aix/` and `.git/` are never allowed. A violation is
recorded as `policy.violation` and fails the attempt (`scope_violation`).

**Policy engine** (`security/policy.py`). Decides which agents may take a task, what permissions an
agent gets, which commands the control plane may run, and which actions need approval. Its hash
(policy version + effective security config) is stored in every decision record and artifact.
A `high`-risk task in `local` mode is only routed to an agent whose own CLI enforces the
restrictions the task depends on:

| Agent | Enforces natively |
|---|---|
| claude | read-only, write scope |
| codex | read-only, network deny |
| gemini | read-only |
| opencode | read-only |
| ollama, fake | everything (they cannot write / are in-process) |

If no eligible agent exists the run fails with `no_eligible_agent` instead of running unconfined.
Permission-to-flag translation for each adapter is snapshot-tested
(`tests/fixtures/permissions/`), and no adapter ever passes a "skip permissions" flag.

**Approvals** (`security/approvals.py`). Gated actions (`approval_required_for`: deploy, migrations,
secrets writes, push, deleting outside scope) create a pending approval and the task waits.
`aix approve|deny` needs an interactive TTY confirmation (typing the short id back) or `--token`
matching `~/.config/aix/approval_token` (mode 0600). Every agent process gets `AIX_AGENT_CONTEXT=1`
and `aix approve`, `aix deny` and `aix approvals --show-token` refuse when it is set. The token is
never placed in an agent environment or prompt.

**Tool-call inspection.** The control plane watches each agent's tool calls. One that touches
`aix approve|deny`, the approval token, the aix state directory, or irreversible git actions is
recorded as `policy.violation` and the attempt fails with `policy_failure`, which is not retried
(the escalation ladder ends at a human). `tests/security/test_prompt_cannot_bypass_policy.py` runs a
scripted agent that attempts exactly this and asserts nothing was granted and everything recorded.

**Secrets.** Agents get a minimal environment (PATH, HOME, LANG, LC_ALL, TERM) plus their adapter's
allowlisted variables; everything else is dropped. Redaction removes key-shaped strings by pattern
and the *values* of credential-named environment variables plus the approval token, in events,
artifacts, prompts, patches, raw agent streams and check output. `tests/security/test_secret_leak.py`
scans every file under `.aix/` (database, streams, prompts, artifacts, unzipped bundle) after a run
whose agent echoes a key everywhere, and fails if redaction is disabled.

**Verification commands.** Run without a shell, only if the executable is in `security.shell_allow`,
in a minimal environment, with `network: deny` best-effort (proxy variables point at a closed port,
package managers are told to stay offline).

**Decision integrity.** Decision state is built only from control-plane facts (check results,
diff statistics, attempt history), with length and content restrictions; agent claims are labeled
"unverified" wherever they appear. Hard gates run in code before any provider (rules or Jev) and
cannot be overridden.

## 3. What `local` mode does not protect

- **Same-user access.** An agent can read anything you can: your home directory, SSH keys, the
  approval token file, the aix database. Tool-call inspection *records* an attempt to read the token
  but cannot prevent it. Agents cannot approve (the guard refuses in agent context) but a
  determined process that read the token could run `aix approve` outside an agent context.
- **Writes outside the worktree** by absolute path are invisible to the diff-based scope check.
- **Detection is stream-based.** Only tool calls the agent's CLI reports are inspected. An action
  hidden inside a script the agent writes and runs, or obfuscated (base64, split commands), is not
  seen.
- **Scope is not native for codex, gemini, opencode.** Only the after-the-fact diff check applies;
  a file changed and changed back is not seen.
- **Network.** Agents talk to their providers by design. Nothing filters what they send.
  `network_deny` is enforced natively only by codex.
- **Data leaves your machine.** Prompts and repository excerpts go to each agent's provider. If
  Jev is enabled, its decision state (task ids, check results and statistics, never code, prompts
  or agent claims) goes to TypeSafe AI.

## 4. Container mode (`security.sandbox: container`)

Attempts run under docker/podman: only the worktree is mounted (read-write), no home directory, no
aix database, no approval token; as your uid/gid, all capabilities dropped, `no-new-privileges`,
read-only root filesystem, memory and pid limits. Credentials are forwarded by name, so they never
appear in `ps`. Planner and reviewer agents run the same way. Tested against a real container.

Known gaps:

- **No egress allowlist.** `network: none` blocks everything (agents needing a cloud API cannot
  work); `network: bridge` allows all egress. Provider-only egress via a filtering proxy is not
  implemented, so the spec's "egress restricted to provider endpoints" is **not met**.
- **You supply the image**, containing your agent CLI; aix does not build or vet it.
- `git` inside the container cannot see the repository object store (only the worktree is mounted);
  diffs are computed on the host.
- Verification checks and `aix review` still run on the host, not in the container.
- A container escape or a docker-group user is out of scope.
- Without a runtime, or without the image, container runs fail before anything is recorded.

## 5. Integrity of results

- Artifacts are content-addressed and redacted; `manifest.json` lists every artifact with its hash.
  `aix artifact verify` re-hashes a bundle. The bundle is **not signed**: someone who rewrites a file
  and both manifest files is not detected unless you compare `manifest.sha256` with the manifest
  hash recorded in the event store.
- The event store is append-only by trigger but is an ordinary SQLite file: anyone with write access
  can replace the whole file. It is not encrypted and contains goals and (redacted) agent summaries.
- Crash recovery treats a recycled process id as a live orchestrator (the safe side); delete
  `.aix/runs/<run>/orchestrator.pid` to force a resume.

## 6. Prompt injection

Repository text and agent output are untrusted. They reach prompts as labeled excerpts (project
facts, dependency handoffs with the agent claim quoted under `AGENT CLAIM (unverified)`) and
cannot forge fact lines. No decision, gate or verification result is derived from agent prose. This
limits what an injected instruction can achieve *inside aix*; it does not stop an injected agent
from misusing whatever access its own CLI grants (section 3).

## 7. Known limitations of the implementation

- The legacy single-agent path (`aix run --agent <id>`) records tool-call violations but does not
  fail the attempt on them, and writes no artifacts.
- The Jev provider has been tested against a scripted fake client, not the live service.
- The ollama adapter has been tested against a scripted local server, not a real Ollama.
- Redaction is textual: short (under 8 characters) or innocuously-named secrets, and transformed
  secrets, are not caught.

## 8. Reporting

If you find a way around a control described here, please open an issue describing the class of
problem (not a working exploit).
