# Evidence

- `dod-jwt-run-bundle.zip` — bundle of the §31 walk-through run (`tests/golden/test_dod.py`): fixture repo,
  goal "Add JWT authentication to this repository", three fake agents, real toolchain checks, one retry
  after failed tests. 7/7 tasks, 8 decisions, final accept. Check it with `aix artifact verify <zip>`;
  replay the decisions with `aix dev eval-decisions --replay <decision-log.json extracted from the zip>`.
  Regenerate with `AIX_DOD_BUNDLE_OUT=$PWD/docs/playbook/evidence/dod-jwt-run-bundle.zip uv run pytest tests/golden/test_dod.py`.
- The bundle comes from fake agents; no live agent run is attached (see FINAL_REPORT.md for what ran live).
