"""Skill registry (PLAYBOOK §16). Builtin skills ship inside the package."""

from __future__ import annotations

from importlib import resources

from aix.domain.errors import ConfigError
from aix.skills.loader import load_skill
from aix.skills.schema import Skill


class SkillRegistry:
    """Name → :class:`Skill` lookup."""

    def __init__(self, skills: list[Skill] | None = None) -> None:
        self._skills: dict[str, Skill] = {}
        for skill in skills or []:
            self.register(skill)

    @classmethod
    def builtin(cls) -> SkillRegistry:
        """Load every builtin skill; a broken builtin is a packaging bug and raises."""
        root = resources.files("aix.skills.builtin")
        skills = [
            load_skill(entry, entry.name)
            for entry in sorted(root.iterdir(), key=lambda e: e.name)
            if entry.is_dir() and not entry.name.startswith("_")
        ]
        return cls(skills)

    def register(self, skill: Skill) -> None:
        """Add a skill; duplicate names are rejected."""
        if skill.meta.name in self._skills:
            raise ConfigError(f"skill {skill.meta.name!r} is already registered")
        self._skills[skill.meta.name] = skill

    def names(self) -> list[str]:
        """Sorted skill names."""
        return sorted(self._skills)

    def get(self, name: str) -> Skill:
        """Return the skill or raise ``ConfigError`` listing the known names."""
        try:
            return self._skills[name]
        except KeyError:
            raise ConfigError(
                f"unknown skill {name!r}; known: {', '.join(self.names()) or 'none'}"
            ) from None

    def all(self) -> list[Skill]:
        """All skills sorted by name."""
        return [self._skills[n] for n in self.names()]
