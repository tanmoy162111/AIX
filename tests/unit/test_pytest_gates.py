"""Tests for the test-suite guard rails: socket blocker, live gate, anyio backend."""

from __future__ import annotations

import asyncio
import socket

import anyio.lowlevel
import pytest

pytest_plugins = ["pytester"]


def test_external_connect_is_blocked() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="network access blocked"):
            s.connect(("93.184.216.34", 80))
    finally:
        s.close()


def test_external_name_resolution_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="network access blocked"):
        socket.getaddrinfo("example.com", 80)


def test_localhost_is_allowed() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(server.getsockname())
    finally:
        client.close()
        server.close()


def test_unix_sockets_are_allowed(tmp_path: pytest.TempPathFactory) -> None:
    path = str(tmp_path / "s.sock")  # type: ignore[operator]
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(path)
    finally:
        client.close()
        server.close()


@pytest.mark.allow_network
def test_allow_network_marker_disables_blocker() -> None:
    # Resolving localhost proves the guard is off without touching the real network.
    assert socket.getaddrinfo("localhost", 80)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.2)
    try:
        # TEST-NET-1 is unroutable: we expect a timeout/OSError, never the blocker's RuntimeError.
        with pytest.raises(OSError):
            s.connect(("192.0.2.1", 80))
    finally:
        s.close()


@pytest.mark.anyio
async def test_anyio_tests_run_on_asyncio() -> None:
    await anyio.lowlevel.checkpoint()
    assert asyncio.get_running_loop() is not None


LIVE_SRC = """
import pytest

@pytest.mark.live
def test_needs_real_agent():
    pass
"""


def test_live_tests_are_skipped_by_default(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AIX_LIVE", raising=False)
    pytester.makepyfile(LIVE_SRC)
    result = pytester.runpytest_inprocess("-p", "aix_pytest_plugin", "-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*AIX_LIVE=1*"])


def test_live_tests_run_when_aix_live_is_set(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIX_LIVE", "1")
    pytester.makepyfile(LIVE_SRC)
    result = pytester.runpytest_inprocess("-p", "aix_pytest_plugin", "-rs")
    result.assert_outcomes(passed=1)
