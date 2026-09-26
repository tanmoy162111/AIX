"""Suite-wide pytest guard rails (PLAYBOOK §26).

* Blocks non-local network access in every test unless marked ``allow_network`` or ``live``.
* Skips ``live`` tests unless ``AIX_LIVE=1`` (CLAUDE.md §5: no live paid calls by default).
* Pins the anyio backend to asyncio.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Iterator
from typing import Any

import pytest

_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", ""}


def _is_local_host(host: object) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if not isinstance(host, str):
        return False
    if host.lower() in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _blocked(target: object) -> RuntimeError:
    return RuntimeError(
        f"network access blocked in tests: {target!r} "
        "(mark the test `allow_network` or `live`, or use a fake)"
    )


def _address_host(address: Any) -> object:
    return address[0] if isinstance(address, tuple) and address else address


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: requires real agent CLIs/Jev; needs AIX_LIVE=1")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("AIX_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test: set AIX_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _network_guard(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    if request.node.get_closest_marker("allow_network") or request.node.get_closest_marker("live"):
        yield
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: Any) -> None:
        if self.family == socket.AF_UNIX or _is_local_host(_address_host(address)):
            return real_connect(self, address)
        raise _blocked(address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> int:
        if self.family == socket.AF_UNIX or _is_local_host(_address_host(address)):
            return real_connect_ex(self, address)
        raise _blocked(address)

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_local_host(host):
            return real_getaddrinfo(host, *args, **kwargs)
        raise _blocked(host)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield
