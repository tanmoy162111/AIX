"""Entry-point discovery, validation and lazy loading (PLAYBOOK §25)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from aix.domain.errors import ConfigError
from aix.plugins.loader import discover, load_object
from aix.plugins.manifest import PluginManifest, PluginType


def raw(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "sample",
        "version": "0.1.0",
        "type": "check",
        "capabilities": [],
        "permissions": [],
        "requires_aix": ">=0.1",
        "entrypoint": "json:dumps",
    }
    return base | over


@dataclass
class EP:
    name: str
    load_value: Any
    value: str = "pkg.mod:manifest"
    dist_name: str = "aix-sample"

    def load(self) -> Any:
        if isinstance(self.load_value, Exception):
            raise self.load_value
        return self.load_value

    @property
    def dist(self) -> Any:
        return type("Dist", (), {"name": self.dist_name})()


def provider(groups: dict[str, list[EP]]) -> Callable[..., list[EP]]:
    def entry_points(*, group: str) -> list[EP]:
        return groups.get(group, [])

    return entry_points


def found(eps: list[EP], type_: PluginType = PluginType.CHECK, **kw: Any):  # type: ignore[no-untyped-def]
    return discover(
        type_,
        aix_version=kw.pop("aix_version", "0.1.0"),
        entry_points_fn=provider({"aix.checks": eps, "aix.adapters": eps}),
        **kw,
    )


def test_a_mapping_a_model_and_a_factory_are_all_accepted_as_manifests() -> None:
    model = PluginManifest.model_validate(raw(id="b"))
    records = found([EP("sample", raw()), EP("b", model), EP("c", lambda: raw(id="c"))])
    assert [r.id for r in records] == ["b", "c", "sample"]  # sorted by id
    assert all(r.ok for r in records), [r.error for r in records]
    assert records[0].source == "aix-sample (pkg.mod:manifest)"


def test_builtins_come_first_and_are_marked() -> None:
    builtin = PluginManifest.model_validate(raw(id="core-check"))
    records = found([EP("sample", raw())], builtins=[builtin])
    assert (records[0].id, records[0].source) == ("core-check", "builtin")
    assert records[1].id == "sample"


def test_a_broken_plugin_is_reported_and_does_not_hide_the_others() -> None:
    records = found(
        [
            EP("boom", ImportError("no module named ghost")),
            EP("junk", "not a manifest"),
            EP("bad-version", raw(id="bad-version", version="one")),
            EP("sample", raw()),
        ]
    )
    by_id = {r.id: r for r in records}
    assert by_id["sample"].ok
    assert not by_id["boom"].ok and "ghost" in (by_id["boom"].error or "")
    assert not by_id["junk"].ok and by_id["junk"].manifest is None
    assert not by_id["bad-version"].ok and "version" in (by_id["bad-version"].error or "")


def test_the_manifest_must_match_its_entry_point() -> None:
    records = found(
        [EP("alias", raw(id="sample")), EP("wrong-type", raw(id="wrong-type", type="adapter"))]
    )
    by_id = {r.id: r for r in records}
    assert "does not match" in (by_id["alias"].error or "")
    assert "adapter" in (by_id["wrong-type"].error or "") and not by_id["wrong-type"].ok


def test_an_incompatible_aix_version_is_reported_not_loaded() -> None:
    (rec,) = found([EP("sample", raw(requires_aix=">=2.0"))], aix_version="0.1.0")
    assert not rec.ok and "requires aix >=2.0" in (rec.error or "") and "0.1.0" in rec.error  # type: ignore[operator]
    assert rec.manifest is not None  # kept for display


def test_duplicate_ids_keep_the_first_and_report_the_second() -> None:
    builtin = PluginManifest.model_validate(raw(id="sample"))
    records = found([EP("sample", raw())], builtins=[builtin])
    assert [r.ok for r in records] == [True, False]
    assert "duplicate" in (records[1].error or "")


def test_only_the_requested_group_is_scanned() -> None:
    fn = provider({"aix.checks": [EP("sample", raw())]})
    assert discover(PluginType.ADAPTER, aix_version="0.1.0", entry_points_fn=fn) == []


def test_load_object_imports_the_entrypoint_lazily() -> None:
    m = PluginManifest.model_validate(raw(entrypoint="json:dumps"))
    (rec,) = found([EP("sample", m)])
    import json

    assert load_object(rec) is json.dumps
    dotted = PluginManifest.model_validate(raw(entrypoint="os:path.join"))
    (rec2,) = found([EP("sample", dotted)])
    import os

    assert load_object(rec2) is os.path.join


@pytest.mark.parametrize("entrypoint", ["no_such_module_xyz:thing", "json:no_such_attr"])
def test_load_object_failures_are_config_errors(entrypoint: str) -> None:
    (rec,) = found([EP("sample", raw(entrypoint=entrypoint))])
    with pytest.raises(ConfigError, match="sample"):
        load_object(rec)


def test_load_object_refuses_a_record_that_failed_validation() -> None:
    (rec,) = found([EP("sample", raw(requires_aix=">=9"))])
    with pytest.raises(ConfigError, match="requires aix"):
        load_object(rec)
