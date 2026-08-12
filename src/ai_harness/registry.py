"""Model registry access."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .paths import Project, resolve_content

REGISTRY_FILE = "model-registry.yaml"


@dataclass(frozen=True)
class ModelEntry:
    id: str
    tier: str
    cost: str
    provider: str


class Registry:
    def __init__(self, data: dict[str, Any]):
        self._data = data
        self._models: dict[str, ModelEntry] = {}
        for provider, block in (data.get("providers") or {}).items():
            for entry in block.get("models") or []:
                self._models[entry["id"]] = ModelEntry(
                    id=entry["id"], tier=entry.get("tier", "unknown"),
                    cost=entry.get("cost", "unknown"), provider=provider,
                )

    @classmethod
    def load(cls, project: Project | None = None) -> "Registry":
        root = project.root if project else None
        path: Path = resolve_content(REGISTRY_FILE, root)
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")))

    def models(self) -> list[ModelEntry]:
        return list(self._models.values())

    def known(self, model_id: str) -> bool:
        return model_id in self._models

    def validate(self, model_id: str) -> str:
        """Reject anything outside the registry — this is the determinism gate."""
        if not self.known(model_id):
            allowed = ", ".join(sorted(self._models))
            raise ValueError(f"model {model_id!r} is not in the registry; allowed: {allowed}")
        return model_id

    def default_for(self, agent: str) -> str:
        defaults = self._data.get("defaults") or {}
        if agent not in defaults:
            raise KeyError(f"no default model for agent {agent!r}")
        return self.validate(defaults[agent])

    def effort_for(self, agent: str, fallback: str = "high") -> str:
        return (self._data.get("effort") or {}).get(agent, fallback)
