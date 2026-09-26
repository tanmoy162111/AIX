#!/usr/bin/env bash
# PreToolUse: block edits to secrets/lockfile; keep DECISIONS.md append-only.
input=$(cat)
f=$(jq -r '.tool_input.file_path // empty' <<<"$input")
[[ -n "$f" ]] || exit 0
base=$(basename "$f")
case "$base" in
  .env|.env.*)
    [[ "$base" == ".env.example" ]] || { echo "Blocked: never edit .env* files (CLAUDE.md §5)." >&2; exit 2; } ;;
  uv.lock)
    echo "Blocked: uv.lock is managed by 'uv lock' / 'uv sync', not manual edits." >&2; exit 2 ;;
esac
if [[ "$f" == */docs/playbook/DECISIONS.md ]]; then
  tool=$(jq -r '.tool_name' <<<"$input")
  if [[ "$tool" == "Write" ]]; then
    echo "Blocked: DECISIONS.md is append-only; use Edit to append a new ADR, don't overwrite it." >&2; exit 2
  fi
  old=$(jq -r '.tool_input.old_string // ""' <<<"$input")
  new=$(jq -r '.tool_input.new_string // ""' <<<"$input")
  if [[ -n "$old" && "$new" != *"$old"* ]]; then
    echo "Blocked: DECISIONS.md is append-only; the edit removes/alters existing text. new_string must contain old_string." >&2; exit 2
  fi
fi
exit 0
