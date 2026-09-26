# aix playbook kit

Drop these files into an **empty directory** that will become the `aix` repository:

```text
CLAUDE.md                        # operating contract (Claude Code reads this automatically)
docs/playbook/PLAYBOOK.md        # the spec (v2)
docs/playbook/PROGRESS.md        # milestone checklist M0–M10
docs/playbook/DECISIONS.md       # ADR log, seeded with ADR-0001
```

## Start the autonomous build

From that directory:

```bash
claude --permission-mode acceptEdits    # or your preferred autonomous mode
```

Then paste this kickoff prompt once:

> Read CLAUDE.md, then read docs/playbook/PLAYBOOK.md in full, then PROGRESS.md and DECISIONS.md.
> Build aix by following the work loop in CLAUDE.md §2, starting at M0.1, without asking me
> anything. Decide ambiguities with CLAUDE.md §3 and record ADRs. Keep going milestone by milestone
> until PLAYBOOK §31 passes and FINAL_REPORT.md exists. If your context runs low, make sure
> PROGRESS.md is current and committed, then continue from it.

If the session ends before completion, restart with:

> Resume the aix build: read CLAUDE.md and PROGRESS.md, then continue the work loop from the first
> unchecked item.

## Optional, for live validation (M10)
- Install and log in to any of: `claude`, `codex`, `gemini`, `opencode`, `ollama`.
- Export `TYPESAFE_API_KEY` for Jev (console.typesafe.ai); `uv sync --extra jev`.
- Install `semgrep`, `gitleaks`, `pip-audit`, and docker/podman for full verification and sandbox coverage.

Without these, the build still completes: everything is tested against fakes and recorded streams,
and FINAL_REPORT.md states what was validated live vs. fake-only.
