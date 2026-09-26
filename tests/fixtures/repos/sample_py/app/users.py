"""In-memory user store."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


def _hash(password: str) -> str:
    return hashlib.sha256(f"salt:{password}".encode()).hexdigest()


@dataclass(frozen=True)
class User:
    username: str
    password_hash: str


class UserStore:
    """Keeps users in a dict."""

    def __init__(self) -> None:
        self._users: dict[str, User] = {}

    def add(self, username: str, password: str) -> User:
        if username in self._users:
            raise ValueError(f"user exists: {username}")
        user = User(username, _hash(password))
        self._users[username] = user
        return user

    def get(self, username: str) -> User | None:
        return self._users.get(username)

    def verify(self, username: str, password: str) -> bool:
        user = self._users.get(username)
        return user is not None and user.password_hash == _hash(password)
