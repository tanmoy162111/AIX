from __future__ import annotations

import re
from pathlib import Path

import pytest

from aix.domain.enums import TaskType
from aix.domain.errors import ConfigError
from aix.skills.loader import load_skill_dir
from aix.skills.registry import SkillRegistry

BUILTIN = {
    "inspect-repo",
    "feature-implementation",
    "bugfix",
    "code-review",
    "security-review",
    "write-tests",
    "documentation",
}
VENDORS = re.compile(r"claude|codex|gemini|opencode|anthropic|openai|gpt", re.IGNORECASE)


def test_registry_loads_all_seven_builtin_skills() -> None:
    reg = SkillRegistry.builtin()
    assert set(reg.names()) == BUILTIN
    assert reg.names() == sorted(reg.names())


def test_unknown_skill_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="unknown skill 'nope'"):
        SkillRegistry.builtin().get("nope")


@pytest.mark.parametrize("name", sorted(BUILTIN))
def test_builtin_skill_is_coherent(name: str) -> None:
    skill = SkillRegistry.builtin().get(name)
    assert skill.meta.name == name
    assert skill.instructions.strip()
    ids = [s.id for s in skill.workflow.steps]
    assert len(ids) == len(set(ids))
    for step in skill.workflow.steps:
        assert set(step.depends_on) <= set(ids) - {step.id}
        assert step.type in skill.meta.task_types
    assert TaskType.IMPLEMENT not in skill.meta.task_types or any(
        s.file_scope for s in skill.workflow.steps if s.type is TaskType.IMPLEMENT
    )


@pytest.mark.parametrize("name", sorted(BUILTIN))
def test_skills_never_name_a_vendor(name: str) -> None:
    root = Path(__file__).resolve().parents[3] / "src/aix/skills/builtin" / name
    for f in root.iterdir():
        assert not VENDORS.search(f.read_text()), f


@pytest.mark.parametrize("name", ["code-review", "security-review"])
def test_review_skills_require_a_findings_block(name: str) -> None:
    text = SkillRegistry.builtin().get(name).instructions
    assert '"findings"' in text and "severity" in text and "confidence" in text


def test_feature_workflow_shape() -> None:
    wf = SkillRegistry.builtin().get("feature-implementation").workflow
    by_id = {s.id: s for s in wf.steps}
    assert wf.steps[0].type is TaskType.INSPECT
    assert by_id["test"].depends_on == ["implement"] == by_id["review"].depends_on
    assert by_id["security_review"].when_risk_at_least == "medium"


def _write(d: Path, **over: str) -> Path:
    files = {
        "skill.yaml": "name: demo\ndescription: d\ntask_types: [inspect]\n",
        "instructions.md": "do it\n",
        "workflow.yaml": "steps:\n  - {id: a, type: inspect, title: t, goal: g}\n",
        "verification.yaml": "required: []\n",
    }
    files.update(over)
    d.mkdir(parents=True, exist_ok=True)
    for n, t in files.items():
        (d / n).write_text(t)
    return d


def test_loader_accepts_minimal_skill(tmp_path: Path) -> None:
    assert load_skill_dir(_write(tmp_path / "demo")).meta.name == "demo"


@pytest.mark.parametrize(
    ("file", "text", "msg"),
    [
        ("skill.yaml", "name: demo\n", "skill.yaml"),
        ("skill.yaml", "name: Bad Name\ndescription: d\ntask_types: [inspect]\n", "skill.yaml"),
        ("skill.yaml", "name: other\ndescription: d\ntask_types: [inspect]\n", "directory"),
        ("workflow.yaml", "steps: []\n", "workflow.yaml"),
        (
            "workflow.yaml",
            "steps:\n  - {id: a, type: inspect, title: t, goal: g, depends_on: [zz]}\n",
            "unknown step",
        ),
        (
            "workflow.yaml",
            "steps:\n  - {id: a, type: inspect, title: t, goal: g, depends_on: [b]}\n"
            "  - {id: b, type: inspect, title: t, goal: g, depends_on: [a]}\n",
            "cycle",
        ),
        ("workflow.yaml", "steps:\n  - {id: a, type: bogus, title: t, goal: g}\n", "workflow.yaml"),
        ("verification.yaml", "required: [nonsense]\n", "verification.yaml"),
        ("instructions.md", "  \n", "instructions.md"),
        ("skill.yaml", ": : :", "skill.yaml"),
    ],
)
def test_loader_rejects_invalid_files(tmp_path: Path, file: str, text: str, msg: str) -> None:
    with pytest.raises(ConfigError, match=msg):
        load_skill_dir(_write(tmp_path / "demo", **{file: text}))


def test_missing_file_is_named(tmp_path: Path) -> None:
    d = _write(tmp_path / "demo")
    (d / "workflow.yaml").unlink()
    with pytest.raises(ConfigError, match=r"workflow\.yaml"):
        load_skill_dir(d)
