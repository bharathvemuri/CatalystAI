"""Shared helpers for command implementations."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, NoReturn

from ..events import EventLog
from ..paths import Project, find_project_root
from ..state import render


class CommandError(Exception):
    """A user-facing failure. The CLI prints it and exits non-zero."""


def die(message: str) -> NoReturn:
    raise CommandError(message)


def require_project(start: Path | None = None) -> Project:
    root = find_project_root(start)
    if root is None:
        die("no harness project found here or in any parent directory.\n"
            "Run `harness init` in your target repository first.")
    return Project(root)


def open_log(project: Project) -> EventLog:
    return EventLog(project.events)


def sync_state(project: Project) -> dict[str, Any]:
    """Re-render the derived view after appending events."""
    return render(open_log(project), project.state_file)


def read_config(project: Project) -> dict[str, Any]:
    if not project.config_file.exists():
        return {}
    return json.loads(project.config_file.read_text(encoding="utf-8"))


def write_config(project: Project, config: dict[str, Any]) -> None:
    project.config_file.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def info(message: str = "") -> None:
    print(message)


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")
