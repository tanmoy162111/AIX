# CLAUDE.md — Operating Contract for Building `aix`

You are building **aix**, a Universal AI Agent Control Plane, **autonomously**. The human will not
answer questions during the build. This file tells you how to work. The full specification is in
`docs/playbook/PLAYBOOK.md` — read it completely before writing any code, and re-read the section for
a milestone before starting that milestone.

## 1. Source of truth (in priority order)

1. `docs/playbook/PLAYBOOK.md` — the spec. Section numbers are stable; cite them in commits.
2. `docs/playbook/DECISIONS.md` — architecture decisions you have made (ADRs). Append-only.
3. `docs/playbook/PROGRESS.md` — the milestone checklist and your running status. Always current.
4. The code and its tests.

If the spec and reality conflict (e.g. an agent CLI flag changed), reality wins: adapt, then record
an ADR explaining the deviation. Never silently diverge from the spec.

## 2. The work loop (repeat until PROGRESS.md is fully checked)

1. Read `PROGRESS.md`. Pick the **first unchecked item** in the **current milestone**. Do not skip ahead.
2. Re-read the PLAYBOOK section(s) that item references.
3. Write or extend tests first (unit, then integration where the item says so).
4. Implement the smallest change that makes the tests pass.
5. Run `make check` (format, lint, typecheck, tests, import-layer contracts). It must be green.
6. Commit with a Conventional Commit message referencing the item: `feat(router): capability scoring [M3.2, §12]`.
7. Tick the item in `PROGRESS.md`, add a one-line note (what, any caveat), commit that too.
8. When every item in a milestone is ticked, run that milestone's **Exit Gate** commands exactly as
   written in PLAYBOOK §30. Only when all pass, mark the milestone done and tag: `git tag m<N>-done`.

Never mark an item done with failing, skipped-without-reason, or deleted tests.

## 3. Deciding without the human

When something is ambiguous, do not stop. Apply this rule:

> Choose the option that is **simplest, reversible, and keeps the core provider-agnostic**. Record it
> as an ADR in `DECISIONS.md` (context, options, decision, consequences) and continue.

Record an ADR for: any dependency added, any schema change after M1, any deviation from the spec,
any external CLI/API behavior you discovered that differs from the spec, any security trade-off.

## 4. When you are blocked by the outside world

| Situation | Action |
|---|---|
| An agent CLI (claude, codex, gemini, opencode) is not installed or not authenticated | Build and test the adapter against recorded fixtures + the `fake` agent. Mark live tests `@pytest.mark.live` (skipped unless `AIX_LIVE=1`). Note it in PROGRESS. Continue. |
| `TYPESAFE_API_KEY` is absent | Build the Jev provider against `FakeJevClient`. Live Jev tests are `@pytest.mark.live`. The rule-based decision provider is the default. Continue. |
| A package cannot be installed | Pick an equivalent, write an ADR, continue. |
| A test is flaky | Fix the root cause (usually time, ordering, or subprocess cleanup). Do not add retries to tests. |
| Stuck on the same failure after 3 genuinely different attempts | Write the problem to `PROGRESS.md` under **Blocked**, stub the smallest interface that lets later work proceed, mark the item `[~]` (partial), move to the next item. Come back at the end of the milestone. |

## 5. Hard rules (never break these)

- Never push to a remote, publish a package, open PRs, or deploy anything.
- Never write outside the repository directory, except `~/.cache` for tooling and the OS temp dir for tests.
- Never commit secrets, API keys, or real user tokens. `.env*` is gitignored. Tests use fake keys.
- Never run live paid agent/Jev calls in the default test suite. Live calls only when `AIX_LIVE=1`.
- Never make the core import a vendor adapter. Enforced by `import-linter` (PLAYBOOK §5.3).
- Never treat agent text as verification. Verification = executed checks + recorded evidence.
- Never weaken a test to make it pass. Never delete a test without an ADR.
- Never use `git push`, `git reset --hard` on commits that exist in a tag, or force operations on tags.
- Keep `main` green: every commit must pass `make check`.

## 6. Commands you will use

```bash
uv sync --all-extras          # install
make check                    # fmt-check + lint + typecheck + layers + tests (the gate)
make test                     # tests only
make golden                   # golden scenarios with fake agents (PLAYBOOK §28)
AIX_LIVE=1 make test-live     # live agent/Jev tests (only if CLIs/keys present)
uv run aix --help             # the product
```

## 7. Style

- Python 3.12+, fully typed, `pyright --strict` on `src/aix/core`, `src/aix/domain`, `src/aix/decision`.
- Pydantic v2 models for every schema that crosses a boundary. No bare dicts across modules.
- Async I/O with `anyio`. No blocking subprocess calls in the event loop.
- Small modules, explicit dependencies passed in (no global singletons except the settings loader).
- Every public function in `core/` and `decision/` has a docstring stating its contract.
- Errors are typed (`aix.domain.errors`), and every failure maps to a `FailureClass` (PLAYBOOK §19).

## 8. Finishing

The build is complete when PLAYBOOK §31 (Definition of Done) passes end-to-end with fake agents in
`make golden`, all milestones are tagged, and `docs/playbook/FINAL_REPORT.md` exists summarizing:
what was built, what runs live vs. fake-only, open risks, and every ADR title.
