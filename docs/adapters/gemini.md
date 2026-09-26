# Adapter: gemini (Gemini CLI)

- **Binary tested:** `gemini-cli 0.55.1`, flags discovered with `gemini --help` and the event schema
  read from the installed CLI's own bundle (`JsonStreamEventType`, `StreamJsonFormatter`,
  `emitFinalResult`, the exit-code table) on **2026-09-26**. `aix agent inspect gemini` probes
  `ready` against the real binary. No paid call was made.
- **Verification level:** flags and event field names verified against the CLI source. The
  **recordings are synthetic** (`tests/fixtures/recordings/gemini/meta.yaml` says `synthetic: true`).
  Replace with `aix dev record gemini` (M10.1). Resume by session id is **unverified** live
  (see below).

## Invocation

```text
gemini --output-format stream-json --approval-mode <plan|auto_edit> --skip-trust
       [--model <m>] [--resume <session>] --prompt "Follow the task given on standard input."
```
The task prompt is written to **stdin**: `-p` is documented as "appended to input on stdin", so the
`--prompt` value is a fixed, non-sensitive instruction and the real prompt never appears in argv or
`ps`. `--skip-trust` is needed in headless mode for folders the CLI does not already trust; the
worktrees aiX creates are fresh directories. `--yolo` / `--approval-mode yolo` is never passed.

Probe: `gemini --version`; `gemini --help` must contain `--prompt --output-format --approval-mode
--model --skip-trust --resume`, else health is `degraded` with the missing flags as reason.
`auth_check: prompt_smoke` is a paid call, run only by `aix agent test` / live tests.

## Permission mapping

| Policy | Flags |
|---|---|
| `read_only` | `--approval-mode plan` (the CLI's read-only mode) |
| default write task | `--approval-mode auto_edit` (edit tools auto-approved) |
| `network`, `write_scope`, `allowed_tools` | **not expressible reliably**: `--allowed-tools` is deprecated in favor of policy files. Only the control plane's post-run scope check (§10.4) applies; the policy engine (M8.3) must treat gemini as unable to enforce write scope. |

Consequence of `auto_edit`: shell commands are not auto-approved in headless mode, so the agent
cannot run the test suite itself. That is acceptable because verification is executed by the
control plane (§17), never trusted from the agent.

## Event stream -> `AgentEvent`

| CLI event | Normalized |
|---|---|
| `init` (session_id, model) | `started`; session_id becomes `session_ref` |
| `message` role `assistant` (content, delta) | `text`; assistant text since the last tool call is the `claim` (**not evidence**) |
| `message` role `user` | ignored (echo of the prompt) |
| `tool_use` (tool_name, tool_id, parameters) | `tool_call` |
| `tool_result` (tool_id, status success/error) | `tool_result` (`ok` = status is `success`; name looked up from the `tool_use`) |
| `error` (severity, message) | `error` (non-fatal notice, e.g. a retry warning) |
| `result` (status, error, stats) | `usage`, then `finished`; `status: error` also emits `error` |
| unparseable line / non-object JSON | `error` with `malformed: true`; parsing continues |

## Usage and cost

`result.stats.input_tokens` / `output_tokens` (summed over models by the CLI). Gemini reports **no
cost**: `cost_usd` is `None`; M7.5 estimates it and marks it `estimated`.

## Outcome rules

`result.status == "error"`, a non-zero exit, or **no `result` event** is a failure (even if the exit
code is 0). Exit code 41 (`FATAL_AUTHENTICATION_ERROR`) is always `AUTH_FAILURE`; 130 is the CLI's
own cancellation code and only occurs after our `cancel`, which is reported as `cancelled`.

## Failure classification (`adapters/gemini/errors.py`)

| Signal | FailureClass |
|---|---|
| exit 41, `401`, `Please set an Auth method`, `GEMINI_API_KEY`, `API key not valid`, `PERMISSION_DENIED` | AUTH_FAILURE |
| `429`, `RESOURCE_EXHAUSTED`, `quota`, `rate limit` | RATE_LIMITED |
| `input token count ... exceeds`, `context window` | CONTEXT_FAILURE |
| `fetch failed`, `ENOTFOUND`, `ETIMEDOUT`, `socket hang up`, `503`, `overloaded` | NETWORK_FAILURE |
| watchdog timeout | TIMEOUT |
| anything else | AGENT_FAILURE |

## Sessions

`init` reports a `session_id`. The CLI's resume flag is `--resume <index|latest|id>`; the CLI's
session selector (`findSession`) resolves an argument, and aiX passes the session id. **Not yet
confirmed with a live run**, so `session_ref` resume is best effort until M10.1 records a real
session; a failed resume surfaces as a normal failed attempt.

## Environment

`PATH HOME LANG LC_ALL TERM` plus `GEMINI_API_KEY GOOGLE_API_KEY GOOGLE_GENAI_USE_VERTEXAI
GOOGLE_CLOUD_PROJECT GOOGLE_CLOUD_LOCATION GOOGLE_APPLICATION_CREDENTIALS GEMINI_CLI_HOME`.
