"""Shared Pydantic base and common field types for domain models."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints

Risk = Literal["low", "medium", "high"]
"""Risk level used by intents and tasks."""

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""Lowercase hex SHA-256 digest."""

UtcDatetime = AwareDatetime
"""Timezone-aware datetime; naive values are rejected. Times are UTC ISO-8601 (§4)."""


class DomainModel(BaseModel):
    """Immutable, strict base for every domain model (§6: frozen, no unknown fields)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
