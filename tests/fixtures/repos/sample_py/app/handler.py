"""Request handler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.users import UserStore


@dataclass(frozen=True)
class Response:
    status: int
    body: dict[str, Any] = field(default_factory=dict)


def handle_request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    store: UserStore | None = None,
) -> Response:
    """Route a request. Only ``GET /health`` exists at baseline."""
    if method == "GET" and path == "/health":
        return Response(200, {"status": "ok"})
    return Response(404, {"error": "not found"})
