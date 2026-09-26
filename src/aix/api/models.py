"""Request and response bodies of the HTTP API (PLAYBOOK §24)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunCreate(BaseModel):
    """``POST /runs``: the same knobs as ``aix run "<goal>"``."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    skill: str | None = None
    allow_dirty: bool = False
    max_parallel: int | None = Field(default=None, ge=1)
    budget_usd: float | None = Field(default=None, ge=0.0)
    decision_provider: Literal["rules", "jev"] | None = None


class RunStarted(BaseModel):
    run_id: str
    status: str
    branch: str


class RunView(BaseModel):
    run_id: str
    goal: str
    status: str
    planner: str
    branch: str
    counts: dict[str, int]


class RunListItem(BaseModel):
    run_id: str
    goal: str
    status: str
    created_at: str
    finished_at: str | None


class TaskView(BaseModel):
    task_id: str
    number: int
    type: str
    title: str
    status: str
    agent: str | None
    attempts: int
    depends_on: list[int]
    failure: str | None


class ApprovalDecision(BaseModel):
    """``POST /approvals/{id}``."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["grant", "deny"]
    reason: str | None = None


class ApprovalResult(BaseModel):
    approval_id: str
    decision: Literal["grant", "deny"]
    run_id: str


class CancelResponse(BaseModel):
    run_id: str
    result: str
    status: str
