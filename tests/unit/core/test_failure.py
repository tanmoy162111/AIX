from __future__ import annotations

import pytest

from aix.core.failure import Classified, candidates_for, classify_attempt, classify_verification
from aix.domain.enums import CheckKind as K
from aix.domain.enums import FailureClass as F
from aix.domain.enums import VerificationFailureKind as V
from aix.domain.ids import IdPrefix, new_id
from aix.domain.verification import Check, VerificationReport, compute_overall

VF = F.VERIFICATION_FAILURE


def chk(kind: K, status: str, required: bool = True) -> Check:
    return Check(
        id=new_id(IdPrefix.CHECK), kind=kind, status=status,  # type: ignore[arg-type]
        required=required, summary="s",
    )  # fmt: skip


def rep(*checks: Check) -> VerificationReport:
    return VerificationReport(
        attempt_id=new_id(IdPrefix.ATTEMPT), checks=list(checks), overall=compute_overall(checks)
    )


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([chk(K.TESTS, "failed")], Classified(VF, V.TESTS)),
        ([chk(K.BUILD, "failed")], Classified(VF, V.BUILD)),
        ([chk(K.LINT, "failed")], Classified(VF, V.LINT)),
        ([chk(K.TYPECHECK, "error")], Classified(VF, V.TYPECHECK)),
        ([chk(K.SECRETS, "failed")], Classified(VF, V.SECURITY)),
        ([chk(K.SECURITY_SAST, "failed")], Classified(VF, V.SECURITY)),
        ([chk(K.DEPS, "failed")], Classified(VF, V.SECURITY)),
        ([chk(K.AI_REVIEW, "failed")], Classified(VF, V.REVIEW)),
        ([chk(K.POLICY, "failed")], Classified(F.POLICY_FAILURE, None)),
        # policy outranks everything; build outranks tests; tests outrank lint
        ([chk(K.TESTS, "failed"), chk(K.POLICY, "failed")], Classified(F.POLICY_FAILURE, None)),
        (
            [chk(K.TESTS, "failed"), chk(K.BUILD, "failed")],
            Classified(VF, V.BUILD),
        ),
        (
            [chk(K.LINT, "failed"), chk(K.TESTS, "failed")],
            Classified(VF, V.TESTS),
        ),
        # optional failures are not what failed the attempt
        ([chk(K.TESTS, "passed"), chk(K.LINT, "failed", required=False)], None),
        ([chk(K.TESTS, "passed")], None),
        # incomplete: a required check could not run
        ([chk(K.TESTS, "passed"), chk(K.LINT, "skipped")], Classified(F.TOOL_FAILURE, None)),
    ],
)
def test_classify_verification(checks: list[Check], expected: Classified | None) -> None:
    assert classify_verification(rep(*checks)) == expected


def test_label_includes_the_sub_kind() -> None:
    assert Classified(VF, V.TESTS).label == "verification_failure:tests"
    assert Classified(F.MERGE_CONFLICT, None).label == "merge_conflict"


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("HTTP 429 Too Many Requests", F.RATE_LIMITED),
        ("401 Unauthorized: bad api key", F.AUTH_FAILURE),
        ("ECONNRESET while fetching", F.NETWORK_FAILURE),
        ("context window exceeded", F.CONTEXT_FAILURE),
        ("no space left on device", F.RESOURCE_FAILURE),
        ("segfault", F.AGENT_FAILURE),
    ],
)
def test_generic_stderr_patterns_are_the_fallback_for_an_unclassified_agent_failure(
    stderr: str, expected: F
) -> None:
    assert classify_attempt(agent_failure=None, agent_failed=True, stderr=stderr) == expected


def test_adapter_specific_classification_wins_over_generic_patterns() -> None:
    got = classify_attempt(agent_failure=F.AUTH_FAILURE, agent_failed=True, stderr="429 rate limit")
    assert got is F.AUTH_FAILURE


def test_a_successful_agent_has_no_failure() -> None:
    assert (
        classify_attempt(agent_failure=None, agent_failed=False, stderr="429 in a log line") is None
    )


def test_candidates_are_evidence_consistent_and_start_with_the_rule_class() -> None:
    assert candidates_for(F.VERIFICATION_FAILURE) == [F.VERIFICATION_FAILURE]
    assert candidates_for(F.AGENT_FAILURE)[0] is F.AGENT_FAILURE
    assert set(candidates_for(F.RATE_LIMITED)) <= {F.RATE_LIMITED, F.NETWORK_FAILURE}
    for f in F:
        cs = candidates_for(f)
        assert cs[0] is f and len(set(cs)) == len(cs)
