"""JSON Schema loading and validation.

Validation is a gate, not a suggestion: nothing the model produces enters the
task directory or the event log without passing its contract first.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .paths import Project, resolve_content


class ContractViolation(Exception):
    def __init__(self, contract: str, errors: list[str]):
        self.contract = contract
        self.errors = errors
        joined = "\n".join(f"  - {e}" for e in errors)
        super().__init__(f"{contract} validation failed:\n{joined}")


@lru_cache(maxsize=None)
def _load(contract: str, root: str | None) -> dict[str, Any]:
    path: Path = resolve_content(f"contracts/{contract}.schema.json",
                                 Path(root) if root else None)
    return json.loads(path.read_text(encoding="utf-8"))


def schema(contract: str, project: Project | None = None) -> dict[str, Any]:
    return _load(contract, str(project.root) if project else None)


def validate(contract: str, instance: Any, project: Project | None = None) -> None:
    validator = Draft202012Validator(schema(contract, project))
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    if errors:
        rendered = [
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in errors
        ]
        raise ContractViolation(contract, rendered)
