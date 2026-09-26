"""Live check against the real Jev API. Skipped unless AIX_LIVE=1 and TYPESAFE_API_KEY is set."""

from __future__ import annotations

import os

import pytest

from aix.decision.jev import choice_q, noul_q
from aix.decision.providers.jev import TypeSafeJevClient

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.fixture(autouse=True)
def _need_key() -> None:
    if not os.environ.get("TYPESAFE_API_KEY"):
        pytest.skip("TYPESAFE_API_KEY not set")


async def test_tiny_choice_and_noul() -> None:
    client = TypeSafeJevClient(timeout_s=20)
    state = {"verification": {"overall": "passed"}, "task": {"risk": "low"}}
    resp = await client.system_one(
        state,
        {
            "completion": choice_q(
                "Given only the verification facts, what should happen to this attempt",
                {"accept": "All required checks passed", "fix_and_retry": "A check failed"},
            ),
            "blocking": noul_q("A warning in the facts should block acceptance"),
        },
    )
    assert resp.answers["completion"].choice in ("accept", "fix_and_retry")
    assert 0.0 <= (resp.answers["blocking"].noul or 0.0) <= 1.0
