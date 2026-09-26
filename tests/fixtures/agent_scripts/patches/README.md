Patches are complete diffs against the `sample_py` baseline commit, so each attempt (which starts
from a fresh worktree) can apply exactly one of them.

| Patch | Effect |
|---|---|
| `hello.diff` | adds `GET /hello` + test; suite passes |
| `auth_v1_broken.diff` | JWT login/`/me`; token expiry not enforced, so its own test fails |
| `auth_v2_fixed.diff` | same feature with expiry enforced; suite passes |
| `planted_secret.diff` | adds `app/config.py` with AWS-documentation example keys (fake) |
| `outside_scope.diff` | also writes `deploy/production.yaml`, outside a `src`/`app` scope |

Regenerate by applying the intended edits to a scratch copy and running `git diff --cached`; run
ruff on the copy first so the patched code stays lint-clean.
