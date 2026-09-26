"""Retry mutations (PLAYBOOK §19.2): what changes between attempts, and why."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from aix.core.failure import Classified
from aix.domain.enums import FailureClass as F
from aix.domain.enums import RetryMutation as M
from aix.domain.enums import VerificationFailureKind as V
from aix.domain.errors import IdenticalRetry
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import Task, VerificationSpec

BACKOFF_S: Final = (30.0, 120.0)
"""Wait before the first and second ``wait_and_retry`` (§19.2)."""


@dataclass(frozen=True)
class Step:
    """One retry step: the mutation and how it affects attempt accounting."""

    mutation: M
    consumes_attempt: bool = True
    wait_s: float = 0.0
    mark_agent_unavailable: bool = False


_CONTEXT = Step(M.SAME_AGENT_WITH_FAILURE_CONTEXT)
_CODE_FAILURE = (
    _CONTEXT,
    Step(M.SWITCH_AGENT),
    Step(M.ADD_RESEARCH_STEP),
    Step(M.ASK_HUMAN),
)
_FINDINGS = (Step(M.SAME_AGENT_WITH_FINDINGS), Step(M.SWITCH_AGENT), Step(M.ASK_HUMAN))
_TRANSIENT = (
    Step(M.WAIT_AND_RETRY, consumes_attempt=False, wait_s=BACKOFF_S[0]),
    Step(M.WAIT_AND_RETRY, consumes_attempt=False, wait_s=BACKOFF_S[1]),
    Step(M.SWITCH_AGENT),
)
_BY_CLASS: Final[dict[F, tuple[Step, ...]]] = {
    F.AGENT_NO_CHANGES: (Step(M.SAME_AGENT_CLARIFIED_PROMPT), Step(M.SWITCH_AGENT)),
    F.TIMEOUT: (Step(M.SPLIT_TASK), Step(M.SWITCH_AGENT)),
    F.RATE_LIMITED: _TRANSIENT,
    F.NETWORK_FAILURE: _TRANSIENT,
    F.AUTH_FAILURE: (Step(M.SWITCH_AGENT, consumes_attempt=False, mark_agent_unavailable=True),),
    F.MERGE_CONFLICT: (Step(M.REBASE_AND_RETRY),),
    F.SCOPE_VIOLATION: (Step(M.SAME_AGENT_WITH_SCOPE_REMINDER), Step(M.SWITCH_AGENT)),
    F.CONTEXT_FAILURE: (Step(M.COMPACT_CONTEXT_AND_RETRY),),
    F.INTERRUPTED: (Step(M.SAME_AGENT_NEW_CONTEXT),),
    F.POLICY_FAILURE: (),
    F.BUDGET_EXCEEDED: (),
    F.HUMAN_REJECTION: (),
    F.NO_ELIGIBLE_AGENT: (),
}
_DEFAULT: Final = (Step(M.SAME_AGENT_NEW_CONTEXT), Step(M.SWITCH_AGENT), Step(M.ASK_HUMAN))


def sequence_for(cls: Classified) -> tuple[Step, ...]:
    """The default mutation sequence for a classified failure (§19.2 table)."""
    if cls.failure is F.VERIFICATION_FAILURE:
        if cls.sub_kind in (V.SECURITY, V.REVIEW):
            return _FINDINGS
        return _CODE_FAILURE
    return _BY_CLASS.get(cls.failure, _DEFAULT)


def next_step(cls: Classified, used: Sequence[M]) -> Step | None:
    """The next mutation given the mutations already used for this failure class on this task.

    ``None`` means the sequence is exhausted (or the class never retries automatically), so the
    caller must ``ask_human`` or fail.
    """
    seq = sequence_for(cls)
    return seq[len(used)] if len(used) < len(seq) else None


def remaining_mutations(cls: Classified, used: Sequence[M]) -> list[str]:
    """Names of the mutations still available, for the ``failure_triage`` question."""
    return [s.mutation.value for s in sequence_for(cls)[len(used) :]]


@dataclass(frozen=True)
class RetryFingerprint:
    """What makes two attempts identical: agent, prompt and the commit they start from."""

    agent_id: str
    prompt_hash: str
    base_commit: str

    @classmethod
    def of(cls, agent_id: str, prompt: str, base_commit: str) -> RetryFingerprint:
        """Fingerprint an attempt; only a hash of the prompt is kept."""
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        return cls(agent_id, digest, base_commit)


class RetryLedger:
    """Per-task record of attempt fingerprints; recording a duplicate raises."""

    def __init__(self) -> None:
        self._seen: set[RetryFingerprint] = set()

    def __len__(self) -> int:
        return len(self._seen)

    def record(self, fp: RetryFingerprint) -> None:
        """Remember ``fp``.

        Raises:
            IdenticalRetry: an earlier attempt had the same agent, prompt hash and base commit.
        """
        if fp in self._seen:
            raise IdenticalRetry(
                f"attempt would repeat an earlier one exactly (agent {fp.agent_id!r})",
                details={"agent_id": fp.agent_id, "base_commit": fp.base_commit},
            )
        self._seen.add(fp)


def split_task(
    task: Task, dependents: Sequence[Task], *, parts: int = 3
) -> tuple[list[Task], list[Task]]:
    """Split a timed-out task into ``parts`` (2 or 3) chained subtasks.

    Returns ``(subtasks, rewired_dependents)``: the first subtask inherits the original's
    dependencies, each next one depends on the previous, dependents now wait for the last, and
    only the last carries the original verification spec. The caller replaces the original.

    Raises:
        ValueError: ``parts`` is not 2 or 3 (§19.2 allows at most three subtasks).
    """
    if not 2 <= parts <= 3:
        raise ValueError("split_task makes 2 or 3 subtasks")
    subs: list[Task] = []
    for i in range(parts):
        subs.append(
            task.model_copy(
                update={
                    "id": new_id(IdPrefix.TASK),
                    "title": f"{task.title} (part {i + 1}/{parts})",
                    "goal": (
                        f"{task.goal}\n\nThis is part {i + 1} of {parts} of a larger task. "
                        "Do only this part, leave the rest for the next parts, and keep the "
                        "tree buildable."
                    ),
                    "depends_on": list(task.depends_on) if i == 0 else [subs[-1].id],
                    "verification": task.verification if i == parts - 1 else VerificationSpec(),
                    "status": task.status.__class__.CREATED,
                }
            )
        )
    last = subs[-1].id
    rewired = [
        d.model_copy(update={"depends_on": [last if x == task.id else x for x in d.depends_on]})
        for d in dependents
    ]
    return subs, rewired
