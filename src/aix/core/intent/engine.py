"""Rule-based intent parsing (PLAYBOOK §13.1)."""

from __future__ import annotations

import re
from typing import Final, Literal, Protocol

from aix.domain.base import Risk
from aix.domain.tasks import Intent, RepoFacts

_Kind = Literal["coding", "research", "review", "security", "devops", "docs", "other"]

_HIGH_RISK: Final = re.compile(
    r"\b(deploy(?:ment|ing)?|production|prod|migrat\w*|delet\w*|drop|purge|wipe|credentials?|"
    r"secrets?|tokens?|api[- ]?keys?|passwords?|force[- ]push|rm -rf|billing|payments?\s+data)\b",
    re.IGNORECASE,
)
_MEDIUM_RISK: Final = re.compile(
    r"\b(auth\w*|security|permissions?|payments?|encrypt\w*|crypto\w*|concurren\w*|"
    r"infra\w*|config\w*|schema|dependenc\w*|upgrade|refactor\w*|rewrite)\b",
    re.IGNORECASE,
)
# Ordered: first match wins. Security beats review beats docs, etc.
_KINDS: Final[tuple[tuple[_Kind, re.Pattern[str]], ...]] = (
    (
        "security",
        re.compile(
            r"\b(security|vulnerabilit\w*|cve|pentest|threat model|audit|leaked? secrets?|"
            r"sast|exploit\w*)\b",
            re.IGNORECASE,
        ),
    ),
    ("review", re.compile(r"\b(review|critique|code review|pr review)\b", re.IGNORECASE)),
    (
        "devops",
        re.compile(
            r"\b(ci|cd|pipeline|github actions|dockerfile|docker|kubernetes|k8s|terraform|helm|"
            r"ansible|deploy\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "docs",
        re.compile(
            r"\b(readme|docs?|documentation|changelog|tutorial)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "coding",
        re.compile(
            r"\b(add|fix|implement|build|create|refactor\w*|rename|remove|change|update|"
            r"bug|feature|support|write tests?|tests?|endpoint|function|class|module|handle)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "research",
        re.compile(
            r"\b(compare|research|investigate|explore|summari[sz]e|explain|evaluate|survey|"
            r"analy[sz]e)\b",
            re.IGNORECASE,
        ),
    ),
)
_PATH: Final = re.compile(
    r"(?<![\w./-])((?:[\w.-]+/)+[\w.-]+\.\w+|[\w-]+\.(?:py|ts|tsx|js|go|rs|md|yaml|yml|toml|json))\b"
)


class IntentEngine(Protocol):
    """Parses a goal into a structured :class:`Intent`."""

    def parse(self, goal: str, repo_facts: RepoFacts) -> Intent:
        """Return the intent for ``goal``; must be deterministic for rule-based engines."""
        ...


class RulesIntentEngine:
    """Keyword/heuristic engine, the always-available fallback for Jev classification (§13.1)."""

    def parse(self, goal: str, repo_facts: RepoFacts) -> Intent:
        """Classify ``goal`` into kind and risk using ordered keyword rules.

        Contract: pure and deterministic; never raises for any string. ``repo_facts`` is accepted
        for interface stability (Jev-assisted engines use it) and is not needed by the rules.
        """
        text = goal.strip()
        return Intent(
            goal=text,
            kind=_kind(text),
            risk=_risk(text),
            target_paths=_paths(text),
        )


def _kind(text: str) -> _Kind:
    for kind, pattern in _KINDS:
        if pattern.search(text):
            return kind
    return "other"


def _risk(text: str) -> Risk:
    if _HIGH_RISK.search(text):
        return "high"
    if _MEDIUM_RISK.search(text):
        return "medium"
    return "low"


def _paths(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for m in _PATH.finditer(text):
        seen.setdefault(m.group(1).rstrip(".,"), None)
    return list(seen)
