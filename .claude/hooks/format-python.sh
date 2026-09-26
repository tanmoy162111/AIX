#!/usr/bin/env bash
# PostToolUse: format + lint-fix edited Python files. Never blocks; no-ops until ruff is installed (M0.3).
f=$(jq -r '.tool_input.file_path // empty')
[[ "$f" == *.py && -f "$f" ]] || exit 0
[[ -f pyproject.toml ]] || exit 0
uv run --quiet ruff format "$f" >/dev/null 2>&1
out=$(uv run --quiet ruff check --fix "$f" 2>&1) || { echo "$out" >&2; exit 2; }
exit 0
