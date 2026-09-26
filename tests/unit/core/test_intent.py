from __future__ import annotations

import pytest

from aix.core.intent.engine import RulesIntentEngine
from aix.domain.tasks import RepoFacts

FACTS = RepoFacts(languages=["python"])
ENGINE = RulesIntentEngine()


@pytest.mark.parametrize(
    ("goal", "kind"),
    [
        ("Add a retry option to the http client", "coding"),
        ("Fix the crash when config is empty", "coding"),
        ("Implement OAuth login flow", "coding"),
        ("Review the changes in src/aix/core", "review"),
        ("Do a security audit of the auth module", "security"),
        ("Scan for vulnerabilities and leaked secrets", "security"),
        ("Update the README and write docs for the CLI", "docs"),
        ("Set up a GitHub Actions CI pipeline", "devops"),
        ("Compare vector databases and summarize tradeoffs", "research"),
        ("hello", "other"),
    ],
)
def test_kind(goal: str, kind: str) -> None:
    assert ENGINE.parse(goal, FACTS).kind == kind


@pytest.mark.parametrize(
    ("goal", "risk"),
    [
        ("Rename a local variable in utils.py", "low"),
        ("Add pagination to the users endpoint", "low"),
        ("Migrate the database schema to add an index", "high"),
        ("Deploy the service to production", "high"),
        ("Delete the old credentials handling", "high"),
        ("Rotate the API token used by the client", "high"),
        ("Change the authentication middleware", "medium"),
        ("Refactor the payment retry logic", "medium"),
    ],
)
def test_risk(goal: str, risk: str) -> None:
    assert ENGINE.parse(goal, FACTS).risk == risk


def test_word_boundaries_avoid_false_positives() -> None:
    assert ENGINE.parse("Add a reviewer badge to the profile page", FACTS).risk == "low"
    assert ENGINE.parse("Fix docstring typos", FACTS).kind == "coding"


def test_target_paths_extracted() -> None:
    i = ENGINE.parse("Fix the bug in src/aix/core/x.py and tests/test_x.py, please.", FACTS)
    assert i.target_paths == ["src/aix/core/x.py", "tests/test_x.py"]


def test_goal_preserved_and_deterministic() -> None:
    g = "  Add a feature  "
    a, b = ENGINE.parse(g, FACTS), ENGINE.parse(g, FACTS)
    assert a == b and a.goal == "Add a feature"
