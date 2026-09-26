.PHONY: fmt fmt-check lint typecheck layers test golden test-live check

RUN := uv run

fmt:
	$(RUN) ruff check --fix-only .
	$(RUN) ruff format .

fmt-check:
	$(RUN) ruff format --check .

lint:
	$(RUN) ruff check .

typecheck:
	$(RUN) pyright

layers:
	$(RUN) lint-imports

# Everything not marked `live` (live tests are skipped unless AIX_LIVE=1).
test:
	$(RUN) pytest -q

# Golden scenarios with fake agents (PLAYBOOK §28). Exit code 5 = "no tests collected", which is
# expected until the first scenario lands in M3; it is tolerated here and nowhere else.
golden:
	$(RUN) pytest tests/golden -q; rc=$$?; if [ $$rc -eq 5 ]; then echo "no golden scenarios yet"; exit 0; else exit $$rc; fi

test-live:
	AIX_LIVE=1 $(RUN) pytest tests/live -q -m live

check: fmt-check lint typecheck layers test
