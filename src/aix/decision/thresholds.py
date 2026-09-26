"""Per-point, per-risk decision thresholds (PLAYBOOK §18.1 rule 3, §18.5)."""

from __future__ import annotations

from collections.abc import Mapping

from aix.domain.base import Risk


class Thresholds:
    """Config thresholds keyed ``<point>.<name>``; ``<name>_high`` overrides for high-risk tasks."""

    def __init__(self, values: Mapping[str, float]) -> None:
        self._values = dict(values)

    def get(self, key: str, risk: Risk | None = None) -> float:
        """The value for ``key``; for ``risk == "high"`` the ``<key>_high`` entry wins if present.

        Raises:
            KeyError: neither the key nor (for high risk) its ``_high`` variant is configured.
        """
        if risk == "high" and f"{key}_high" in self._values:
            return self._values[f"{key}_high"]
        return self._values[key]
