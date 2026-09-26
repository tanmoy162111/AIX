"""A credential in the environment never reaches any file aix writes (M8.2, PLAYBOOK §20.5)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import repos
from aix.cli.main import app
from aix.security.redact import configure_known_secrets, known_secret_values, redact_secrets
from cli_env import hermetic
from verif_env import write_fast_config

runner = CliRunner()
KEY = "zk-live-7Hq2VfN8sLp0aXw9TcYb4"  # no known shape: only the env registry can catch it


@pytest.fixture(autouse=True)
def _reset_registry():  # type: ignore[no-untyped-def]
    yield
    configure_known_secrets([])


def test_known_secret_values_by_name_and_length() -> None:
    env = {
        "OPENAI_API_KEY": KEY,
        "MY_TOKEN": "short",
        "PATH": "/usr/bin/verylongpath",
        "X_PASSWORD": "hunter2hunter2",
    }
    assert known_secret_values(env) == [KEY, "hunter2hunter2"]


def test_registered_values_are_removed_everywhere_in_text() -> None:
    configure_known_secrets([KEY])
    assert redact_secrets(f"a {KEY} b {KEY}") == "a [REDACTED] b [REDACTED]"
    configure_known_secrets([])
    assert redact_secrets(f"a {KEY}") == f"a {KEY}"


def test_fake_run_leaves_no_secret_in_any_written_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermetic(tmp_path, monkeypatch)
    monkeypatch.setenv("SOME_SERVICE_API_KEY", KEY)
    proj = repos.materialize_sample_py(tmp_path / "proj")
    assert runner.invoke(app, ["init", "--project", str(proj)]).exit_code == 0
    write_fast_config(proj)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    leaky = {
        "events": [{"kind": "text", "data": {"text": f"my key is {KEY}"}}],
        "claim": f"Done, used {KEY}",
        "stderr": f"warning: {KEY}",
    }
    paths = {
        "implement": "feature.py",
        "test": "tests/test_feature.py",
        "document": "docs/feature.md",
    }
    for name, path in paths.items():
        step = dict(leaky, write_files={path: f"KEY = '{KEY}'\n"})
        (scripts / f"{name}.yaml").write_text(
            yaml.safe_dump({"match": {"task_type": name}, "attempts": [step]})
        )
    monkeypatch.setenv("AIX_FAKE_SCRIPTS", str(scripts))

    res = runner.invoke(app, ["run", "Add a retry option", "--project", str(proj)])
    assert res.exit_code == 0, res.output
    assert KEY not in res.output

    checked = 0
    for path in (proj / ".aix").rglob("*"):
        if not path.is_file() or "worktrees" in path.parts:
            continue
        data = path.read_bytes()
        checked += 1
        assert KEY.encode() not in data, f"secret leaked into {path.relative_to(proj)}"
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    assert KEY.encode() not in zf.read(name), f"leaked into bundle:{name}"
    assert checked > 20  # the scan really covered db, streams, prompts, artifacts, bundle
