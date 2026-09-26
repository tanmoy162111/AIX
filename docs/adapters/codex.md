# Adapter: codex (Codex CLI)

- **Binary tested:** `codex-cli 0.147.0`, flag discovery via `codex exec --help` and
  `codex exec resume --help` on **2026-09-26**.
- **Verification level:** flags verified against the real CLI. The **event stream is synthetic**
  (`tests/fixtures/recordings/codex/*.jsonl`, `meta.yaml` says `synthetic: true`), authored from the
  documented `codex exec --json` event schema. Replace with `aix dev record codex` (M10.1).

## Invocation

```text
codex exec --json -C <workspace> -s <read-only|workspace-write> -c approval_policy="never"
           [-m <model>] [-c sandbox_workspace_write.network_access=true]
           [resume <thread_id>] -
```
`-` makes codex read the prompt from **stdin** (no argv size limit, not visible in `ps`). Approval is
a top-level `codex` flag (`-a`), not an `exec` flag, so it is set with a config override. Nothing
that bypasses sandbox or approvals (`--dangerously-*`) is ever passed.

Probe: `codex --version`; `codex exec --help` must contain `--json --sandbox --model --cd --config`
and the `resume` subcommand; otherwise health is `degraded` with the missing items as reason.
`auth_check: exec_smoke` is a paid call, run only by `aix agent test` / live tests.

## Permission mapping

| Policy | Flags |
|---|---|
| `read_only` | `-s read-only` |
| default write task | `-s workspace-write` |
| `network: allow` | `-c sandbox_workspace_write.network_access=true` (denied by default in workspace-write) |
| `write_scope`, `allowed_tools` | **not expressible**; only the control plane's post-run scope check (§10.4) applies. The policy engine (M8.3) must treat codex as unable to enforce write scope for high-risk tasks. |

Snapshot tests: `tests/adapter/test_codex_parts.py`.

## Event stream -> `AgentEvent`

| CLI event | Normalized |
|---|---|
| `thread.started` (thread_id) | `started`; thread_id becomes `session_ref` |
| `item.started`/`item.completed` of `command_execution`, `file_change`, `mcp_tool_call`, `web_search` | `tool_call` (once per item id) then `tool_result` (`ok` from `exit_code == 0`, else `status == completed`) |
| `item.completed` `agent_message` | `text`; the last one is the `claim` (**not evidence**) |
| `item.completed` `reasoning` | ignored |
| `turn.completed` (usage) | `usage` then `finished` |
| `turn.failed` | `error` then `finished` (fatal) |
| top-level `error` / `item` type `error` | `error` (non-fatal notice) |
| unparseable line / non-object JSON | `error` with `malformed: true`; parsing continues |

## Usage and cost

`usage.input_tokens` and `output_tokens` are summed across `turn.completed` events
(`cached_input_tokens` is a subset of input and is not added). Codex reports **no cost**:
`cost_usd` is `None`; M7.5 estimates it from the price table and marks it `estimated`.

## Failure classification (`adapters/codex/errors.py`)

| Signal | FailureClass |
|---|---|
| `401`, `unauthorized`, `not logged in`, `codex login`, `incorrect api key` | AUTH_FAILURE |
| `429`, `rate limit`, `too many requests`, `quota` | RATE_LIMITED |
| `context_length_exceeded`, `context window`, `maximum context length` | CONTEXT_FAILURE |
| `stream disconnected`, `error sending request`, `dns error`, `connection refused/reset`, `timed out` | NETWORK_FAILURE |
| watchdog timeout | TIMEOUT |
| anything else, or no `turn.completed` with exit 0 | AGENT_FAILURE |

A `turn.failed` or non-zero exit is a failure even if other events looked fine.

## Environment

`PATH HOME LANG LC_ALL TERM` plus `OPENAI_API_KEY OPENAI_BASE_URL CODEX_HOME`.

## Open items for M10 (live)

Confirm: prompt-from-stdin with `-`; that `approval_policy="never"` is honored by `exec`;
`resume <id> -` argument order; whether top-level `error` events are always non-fatal; real item
field names for `file_change`.
