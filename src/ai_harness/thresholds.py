"""Gate thresholds, resolved through the three content tiers.

The framework document defers coverage and performance limits to "the
configured threshold" without saying where that is. Putting them in content
rather than in code means a project tightens or loosens them in
``.harness/overrides/gates/thresholds.yaml`` — a reviewable file in its own
repository — instead of arguing with an agent about what counts as a regression.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from .paths import Project, resolve_content

THRESHOLDS_FILE = "gates/thresholds.yaml"


@dataclass(frozen=True)
class Thresholds:
    data: dict[str, Any]

    @classmethod
    def load(cls, project: Project | None = None) -> "Thresholds":
        path = resolve_content(THRESHOLDS_FILE, project.root if project else None)
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    def section(self, name: str) -> dict[str, Any]:
        return self.data.get(name) or {}

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.section(section).get(key, default)

    @property
    def max_block_cycles(self) -> int:
        return int(self.get("review", "max_block_cycles", 3))

    @property
    def required_gates(self) -> list[str]:
        return list(self.get("gates", "required", ["build", "test"]))

    @property
    def optional_gates(self) -> list[str]:
        return list(self.get("gates", "optional", []))

    @property
    def fail_on_missing_scanner(self) -> bool:
        return bool(self.get("security", "fail_on_missing_scanner", True))

    def gate_is_required(self, gate: str) -> bool:
        return gate in self.required_gates
