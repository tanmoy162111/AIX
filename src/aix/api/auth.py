"""API token scopes (PLAYBOOK §24).

Two secrets live in the user config dir, both mode 0600 and never given to an agent:

* ``api_token``      scopes ``read`` + ``run``  (observe runs, start and cancel them)
* ``approval_token`` scopes ``read`` + ``approve`` (the §20.4 approval token, ``channel=api_token``)

Approving is deliberately a separate credential from running: whoever can start runs cannot also
approve the gated actions those runs request.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from enum import StrEnum

from fastapi import HTTPException, Request

from aix.security.approvals import config_dir, ensure_secret, ensure_token


class Scope(StrEnum):
    READ = "read"
    RUN = "run"
    APPROVE = "approve"


API_SCOPES: tuple[Scope, ...] = tuple(Scope)

TokenTable = Mapping[str, frozenset[Scope]]


def ensure_api_token() -> str:
    """The general API token (``read`` + ``run``), created on first use."""
    return ensure_secret(config_dir() / "api_token")


def default_tokens() -> dict[str, frozenset[Scope]]:
    """The token table ``aix serve`` uses: the API token and the approval token."""
    return {
        ensure_api_token(): frozenset({Scope.READ, Scope.RUN}),
        ensure_token(): frozenset({Scope.READ, Scope.APPROVE}),
    }


def scopes_for(tokens: TokenTable, given: str) -> frozenset[Scope] | None:
    """Scopes granted to ``given``, comparing against every token in constant time."""
    found: frozenset[Scope] | None = None
    for token, scopes in tokens.items():
        if hmac.compare_digest(given.encode(), token.encode()):
            found = scopes
    return found


def require(scope: Scope):  # type: ignore[no-untyped-def]
    """FastAPI dependency: 401 without a valid bearer token, 403 without ``scope``."""

    def dependency(request: Request) -> frozenset[Scope]:
        header = request.headers.get("authorization", "")
        kind, _, given = header.partition(" ")
        challenge = {"WWW-Authenticate": "Bearer"}
        if kind.lower() != "bearer" or not given:
            raise HTTPException(401, "missing bearer token", headers=challenge)
        tokens: TokenTable = request.app.state.tokens
        granted = scopes_for(tokens, given.strip())
        if granted is None:
            raise HTTPException(401, "invalid token", headers=challenge)
        if scope not in granted:
            raise HTTPException(403, f"token lacks the {scope.value!r} scope")
        return granted

    return dependency
