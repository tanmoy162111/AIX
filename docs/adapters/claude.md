# Adapter: claude (Claude Code)

- **Binary tested:** `claude 2.1.283 (Claude Code)`, flag discovery via `claude --help` on **2026-09-26**.
- **Verification level:** flags are verified against the real CLI's help text. The **stream format is
  synthetic**: `tests/fixtures/recordings/claude/*.jsonl` are hand-authored from the documented
  stream-json schema (`meta.yaml` says `synthetic: true`) because live paid calls are off by default.
  Replace them with `aix dev record claude` (M10.1) and re-check the mapping below.

## Invocation

```text
claude -p --output-format stream-json --verbose
       [--model <m>] [--resume <session_id>]
       --permission-mode <acceptEdits|dontAsk>
       [--allowedTools "<comma list>"] [--disallowedTools "<comma list>"]
```
The prompt is written to **stdin** (no argv size limit, not visible in `ps`). cwd = the attempt
worktree. stdout is JSONL; stderr is kept as a 256 KiB tail.

Probe (`aix agent list/inspect`): `claude --version`, and `claude --help` must contain
`--output-format --verbose --permission-mode --allowedTools --disallowedTools --model --resume`;
otherwise health is `degraded` with the missing flags as reason. `auth_check: exec_smoke` is a paid
call and only runs from `aix agent test` / live tests.

## Permission mapping (`AgentPermissions` -> flags)

| Policy | Flags |
|---|---|
| default write task | `--permission-mode acceptEdits` (+ `--allowedTools` if tools given) |
| `read_only` | `--permission-mode dontAsk --allowedTools Read,Glob,Grep --disallowedTools Edit,Write,NotebookEdit,Bash` |
| `write_scope` globs | `--permission-mode dontAsk --allowedTools Read,Glob,Grep,Edit(<g>),Write(<g>)...` (native path rules, best effort) |
| `network: deny` | adds `--disallowedTools WebFetch,WebSearch` |

`dontAsk` denies anything that would prompt, so the run never blocks on a question. Scope is always
re-checked by the control plane after execution (§10.4); the native rules are defense in depth.
Snapshot tests: `tests/adapter/test_claude_parts.py`.

## Stream events -> `AgentEvent`

| CLI event | Normalized |
|---|---|
| `system` / `init` (session_id, model) | `started` |
| `assistant` content `text` | `text` |
| `assistant` content `tool_use` | `tool_call` (name, id, input truncated to 500 chars) |
| `user` content `tool_result` | `tool_result` (tool_use_id, is_error) |
| `result` | `usage` then `finished` |
| unparseable line / non-object JSON | `error` with `malformed: true`; parsing continues |
| any other `type` | ignored |

## Outcome, usage, session

- `claim` = `result.result` (agent's words, **not evidence**).
- Usage: `input_tokens` = input + cache_creation + cache_read; `output_tokens`; `cost_usd` =
  `total_cost_usd` (reported, `estimated=false`); model from `init`.
- Session: `session_id` from `init`/`result` becomes `session_ref`; `resume()` adds `--resume <id>`.
- No `result` event: exit 0 -> `failed` (AGENT_FAILURE, "stream ended without a result event");
  non-zero -> classified from stderr.

## Failure classification (`adapters/claude/errors.py`)

| Signal | FailureClass |
|---|---|
| subtype `error_max_budget_usd` | BUDGET_EXCEEDED |
| `invalid api key`, `please run /login`, `oauth token`, `authentication_error`, `unauthorized`, `401` | AUTH_FAILURE |
| `rate_limit`, `429`, `usage limit`, `overloaded`, `529`, `too many requests` | RATE_LIMITED |
| `ECONNRESET`, `ENOTFOUND`, `ETIMEDOUT`, `network error`, `fetch failed` | NETWORK_FAILURE |
| `prompt is too long`, `context window` | CONTEXT_FAILURE |
| watchdog timeout | TIMEOUT |
| anything else (incl. `error_max_turns`) | AGENT_FAILURE |

## Environment

Only `PATH HOME LANG LC_ALL TERM` plus the manifest allowlist reach the process:
`ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR`.

## Open items for M10 (live)

Confirm: stdin prompt delivery with `-p`; `Edit(<glob>)` rule syntax under `dontAsk`; final `result`
field names; whether `--verbose` is still required for stream-json.
