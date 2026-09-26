"""Builders for valid domain objects, shared by tests. Every builder accepts overrides."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aix.domain.agents import AgentSpec, AgentSupports
from aix.domain.artifacts import Artifact, Producer, Provenance
from aix.domain.decisions import Approval, DecisionRecord, GateResult
from aix.domain.enums import (
    ArtifactType,
    AttemptStatus,
    Capability,
    CheckKind,
    DecisionOutcome,
    DecisionPoint,
    RunStatus,
    TaskStatus,
    TaskType,
)
from aix.domain.execution import Attempt, DiffSummary, ExecutionResult, Usage
from aix.domain.ids import IdPrefix, new_id
from aix.domain.runs import Budget, Run
from aix.domain.tasks import Intent, Task, TaskGraph, VerificationSpec
from aix.domain.verification import Check, VerificationReport

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
SHA = "a" * 64


def intent(**kw: Any) -> Intent:
    return Intent.model_validate(
        {
            "goal": "Add a hello endpoint",
            "kind": "coding",
            "risk": "low",
            "constraints": [],
            "target_paths": [],
            **kw,
        }
    )


def task(run_id: str, **kw: Any) -> Task:
    data: dict[str, Any] = {
        "id": new_id(IdPrefix.TASK),
        "run_id": run_id,
        "title": "Implement",
        "goal": "Implement the thing",
        "type": TaskType.IMPLEMENT,
        "skill": None,
        "required_capabilities": [Capability.IMPLEMENT],
        "depends_on": [],
        "file_scope": ["src/**"],
        "verification": VerificationSpec(required=[CheckKind.TESTS], optional=[]),
        "risk": "low",
        "max_attempts": 3,
        "status": TaskStatus.CREATED,
    }
    data.update(kw)
    return Task.model_validate(data)


def graph(run_id: str, tasks: list[Task] | None = None, **kw: Any) -> TaskGraph:
    return TaskGraph.model_validate(
        {
            "id": new_id(IdPrefix.GRAPH),
            "run_id": run_id,
            "tasks": [task(run_id)] if tasks is None else tasks,
            **kw,
        }
    )


def run(**kw: Any) -> Run:
    data: dict[str, Any] = {
        "id": new_id(IdPrefix.RUN),
        "project_root": Path("/tmp/proj"),
        "goal": "Add a hello endpoint",
        "intent": None,
        "graph_id": None,
        "status": RunStatus.CREATED,
        "budget": Budget(),
        "created_at": NOW,
        "finished_at": None,
    }
    data.update(kw)
    return Run.model_validate(data)


def agent_spec(**kw: Any) -> AgentSpec:
    data: dict[str, Any] = {
        "id": "fake",
        "name": "Fake",
        "kind": "cli",
        "version": None,
        "capabilities": {Capability.IMPLEMENT: 0.8},
        "supports": AgentSupports(),
        "models": [],
        "default_model": None,
        "cost_class": "free",
        "health": "ready",
    }
    data.update(kw)
    return AgentSpec.model_validate(data)


def attempt(task_id: str, **kw: Any) -> Attempt:
    data: dict[str, Any] = {
        "id": new_id(IdPrefix.ATTEMPT),
        "task_id": task_id,
        "number": 1,
        "agent_id": "fake",
        "model": None,
        "mutation": None,
        "workspace": Path("/tmp/ws"),
        "base_commit": "b" * 40,
        "status": AttemptStatus.CREATED,
    }
    data.update(kw)
    return Attempt.model_validate(data)


def execution_result(attempt_id: str, **kw: Any) -> ExecutionResult:
    data: dict[str, Any] = {
        "attempt_id": attempt_id,
        "exit_code": 0,
        "status": "completed",
        "failure": None,
        "claim": "Done",
        "diff": DiffSummary(),
        "tool_calls": [],
        "usage": Usage(),
        "duration_ms": 10,
        "stream_path": Path("/tmp/s.jsonl"),
    }
    data.update(kw)
    return ExecutionResult.model_validate(data)


def check(**kw: Any) -> Check:
    data: dict[str, Any] = {
        "id": new_id(IdPrefix.CHECK),
        "kind": CheckKind.TESTS,
        "status": "passed",
        "severity": "info",
        "required": True,
        "summary": "42 passed",
        "metrics": {"tests_total": 42.0},
        "evidence": [],
        "command": ["pytest", "-q"],
        "duration_ms": 5,
    }
    data.update(kw)
    return Check.model_validate(data)


def report(attempt_id: str, checks: list[Check], overall: str) -> VerificationReport:
    return VerificationReport(attempt_id=attempt_id, checks=checks, overall=overall)  # type: ignore[arg-type]


def decision(**kw: Any) -> DecisionRecord:
    data: dict[str, Any] = {
        "id": new_id(IdPrefix.DECISION),
        "point": DecisionPoint.TASK_COMPLETION,
        "subject": new_id(IdPrefix.ATTEMPT),
        "provider": "rules",
        "gate_result": GateResult(allowed_outcomes=[DecisionOutcome.ACCEPT]),
        "state": {"overall": "passed"},
        "questions": {},
        "answers": {},
        "outcome": DecisionOutcome.ACCEPT,
        "choice": None,
        "reason_codes": ["rules:passed"],
        "provider_meta": {},
        "inputs_hash": SHA,
    }
    data.update(kw)
    return DecisionRecord.model_validate(data)


def approval(**kw: Any) -> Approval:
    data: dict[str, Any] = {
        "id": new_id(IdPrefix.APPROVAL),
        "subject": new_id(IdPrefix.TASK),
        "action": "deploy",
        "scope": {},
        "requested_at": NOW,
        "status": "pending",
        "actor": None,
        "channel": None,
        "decided_at": None,
    }
    data.update(kw)
    return Approval.model_validate(data)


def artifact(run_id: str, **kw: Any) -> Artifact:
    data: dict[str, Any] = {
        "id": new_id(IdPrefix.ARTIFACT),
        "type": ArtifactType.PLAN,
        "media_type": "application/json",
        "sha256": SHA,
        "size": 10,
        "path": Path(".aix/artifacts/objects/aa/" + SHA),
        "provenance": Provenance(
            run_id=run_id,
            producer=Producer(kind="control_plane", id="aix"),
            aix_version="0.1.0",
            created_at=NOW,
        ),
    }
    data.update(kw)
    return Artifact.model_validate(data)
