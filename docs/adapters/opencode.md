# Adapter: opencode (OpenCode)

- **Binary tested:** `opencode 1.18.26`, flags discovered with `opencode run --help`; the event
  emission and permission handling read from the `run` command handler embedded in the installed
  binary on **2026-09-26**. `aix agent inspect opencode` probes `ready` against the real binary.
  No model call was made.
- **Verification level:** flags, event *type names*, stdin handling and permission behavior verified
  against the CLI's own code. The **recordings are synthetic** (`meta.yaml`); the fields inside a
  `part` (`tokens`, `cost`, `reason`, `state.status`, `callID`) follow opencode's message-part schema
  and are **unconfirmed by a live capture**. Replace with `aix dev record opencode` (M10.1).

## Invocation

```text
opencode run --format json --dir <workspace> [--agent plan] [--model <provider/model>]
             [--session <id>]                       # prompt on stdin
```
`run` joins its message arguments with any piped stdin and rejects an empty combination, so the
task goes on **stdin** and no positional message is passed (no argv size limit, nothing in `ps`).
`--auto` (and the hidden `--yolo` / `--dangerously-skip-permissions`) is never passed.

Probe: `opencode --version`; `opencode run --help` must contain `--format --model --session --dir
--agent`, else health is `degraded` with the missing flags as reason. `auth_check: run_smoke` is a
paid call, run only by `aix agent test` / live tests.

## Permission mapping

| Policy | Flags / behavior |
|---|---|
| `read_only` | `--agent plan` (built-in primary agent that denies edits) |
| default write task | default `build` agent. Permissions the config leaves at `ask` (for example paths outside the workspace) are **auto-rejected** in `run` mode, which keeps the agent inside the worktree. |
| `network`, `write_scope`, `allowed_tools` | **not expressible**; only the control plane's post-run scope check (§10.4) applies. The policy engine (M8.3) must treat opencode as unable to enforce write scope. |

When a permission is auto-rejected the CLI prints `!  permission requested: <perm> (<paths>);
auto-rejecting` as a **plain line even with `--format json`**. The parser reports it as an `error`
event with `permission_denied: true` (not as a malformed line) and the outcome's `stderr_tail`
lists it; the attempt is not failed by that alone.

## Event stream -> `AgentEvent`

Every JSON line is `{type, timestamp, sessionID, part | error}`.

| CLI event | Normalized |
|---|---|
| first event of any kind | `started` (session id from `sessionID`, becomes `session_ref`) |
| `text` (`part.text`, emitted when the part is complete) | `text`; the text since the last tool call is the `claim` (**not evidence**) |
| `tool_use` (`part.tool`, `part.state.status` completed or error) | `tool_call` then `tool_result` (`ok` = status `completed`) |
| `step_start` | `started` only |
| `step_finish` (`part.tokens`, `part.cost`, `part.reason`) | `usage` (running totals); `reason != tool-calls` also emits `finished` and marks completion |
| `reasoning` (only with `--thinking`, not passed) | ignored |
| `error` (`error.name`, `error.data.message`) | `error` then `finished` (failed) |
| unparseable line / non-object JSON | `error` with `malformed: true`; parsing continues |

There is **no final result event**: success is "exit 0, no error event, and a final `step_finish`".

## Usage and cost

Tokens are summed over steps: `input_tokens = input + cache.read + cache.write`,
`output_tokens = output + reasoning`. `cost_usd` is the sum of `step_finish.cost` and is `None` when
the total is 0 (opencode reports 0 for models without pricing, which means unknown, not free).

## Failure classification (`adapters/opencode/errors.py`)

| Signal | FailureClass |
|---|---|
| `ProviderAuthError`, `401`, `403`, `unauthorized`, `api key`, `credentials` | AUTH_FAILURE |
| `429`, `rate limit`, `too many requests`, `quota` | RATE_LIMITED |
| `ContextOverflowError`, `prompt is too long`, `context length` | CONTEXT_FAILURE |
| `ENOTFOUND`, `ECONNRESET`, `Unable to connect`, `502/503/504`, `overloaded` | NETWORK_FAILURE |
| watchdog timeout | TIMEOUT |
| anything else, or no final `step_finish` with exit 0 | AGENT_FAILURE |

A session `error` or a non-zero exit is a failure even if other events looked fine.

## Sessions

`--session <id>` continues a session (`Session not found` exits 1 if it does not exist). The id is
the `sessionID` of any event.

## Environment

`PATH HOME LANG LC_ALL TERM` plus provider keys `ANTHROPIC_API_KEY OPENAI_API_KEY GEMINI_API_KEY
GOOGLE_GENERATIVE_AI_API_KEY OPENROUTER_API_KEY GROQ_API_KEY` and `XDG_*` / `OPENCODE_CONFIG*`.
`PWD` is deliberately not forwarded (opencode prefers `$PWD` over the process cwd, so `--dir` is
passed explicitly).
