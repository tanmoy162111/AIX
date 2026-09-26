from __future__ import annotations

import pytest

from aix.domain import errors
from aix.domain.enums import FailureClass, VerificationFailureKind


def _error_classes() -> list[type[errors.AixError]]:
    return [
        c
        for c in vars(errors).values()
        if isinstance(c, type) and issubclass(c, errors.AixError) and c is not errors.AixError
    ]


def test_every_failure_class_has_an_error_type() -> None:
    covered = {c.failure_class for c in _error_classes()}
    assert covered == set(FailureClass)


@pytest.mark.parametrize("cls", _error_classes(), ids=lambda c: c.__name__)
def test_each_error_declares_a_failure_class(cls: type[errors.AixError]) -> None:
    assert isinstance(cls.failure_class, FailureClass)
    err = cls("boom")
    assert str(err) == "boom"
    assert err.failure_class is cls.failure_class
    assert err.details == {}


def test_details_are_kept() -> None:
    err = errors.AgentFailure("crashed", details={"exit_code": 2})
    assert err.details == {"exit_code": 2}


def test_verification_failure_has_subkind() -> None:
    err = errors.VerificationFailed("tests failed", kind=VerificationFailureKind.TESTS)
    assert err.failure_class is FailureClass.VERIFICATION_FAILURE
    assert err.kind is VerificationFailureKind.TESTS


def test_base_class_cannot_be_instantiated_without_class() -> None:
    with pytest.raises(TypeError):
        errors.AixError("x")


def test_classify_maps_foreign_exceptions() -> None:
    assert errors.classify(errors.MergeConflict("c")) is FailureClass.MERGE_CONFLICT
    assert errors.classify(TimeoutError()) is FailureClass.TIMEOUT
    assert errors.classify(ConnectionError()) is FailureClass.NETWORK_FAILURE
    assert errors.classify(PermissionError()) is FailureClass.POLICY_FAILURE
    assert errors.classify(OSError(28, "No space left on device")) is FailureClass.RESOURCE_FAILURE
    assert errors.classify(MemoryError()) is FailureClass.RESOURCE_FAILURE
    assert errors.classify(RuntimeError("?")) is FailureClass.TOOL_FAILURE


def test_illegal_transition_and_config_errors_are_typed() -> None:
    assert issubclass(errors.IllegalTransition, errors.AixError)
    assert issubclass(errors.ConfigError, errors.AixError)
    assert issubclass(errors.UnsupportedError, errors.AixError)
