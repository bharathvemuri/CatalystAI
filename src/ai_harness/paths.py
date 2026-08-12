"""Filesystem layout and three-tier content resolution.

Content in ``core/`` (agent contracts, gate scripts, the model registry) resolves
through three tiers, most specific first:

1. ``<project>/.harness/overrides/``  — per-project customisation, checked in
2. ``$AI_HARNESS_HOME`` or ``~/.ai-harness/`` — user-level, spans every project
3. the installed package's ``core/`` — the defaults shipped with the harness

Tier 2 also owns anything the harness *writes* that must outlive a single
project — notably the institutional-memory lessons log (spec section 12), which
is explicitly cross-project and therefore cannot live in either the package
(wiped on upgrade) or a target repo (per-project by definition).
"""

from __future__ import annotations

import os
from pathlib import Path

HARNESS_DIRNAME = ".harness"


def package_core() -> Path:
    """Defaults shipped inside the installed package."""
    return Path(__file__).resolve().parent / "core"


def user_home() -> Path:
    """User-level harness directory (tier 2). Created on demand."""
    env = os.environ.get("AI_HARNESS_HOME")
    return Path(env).expanduser() if env else Path.home() / ".ai-harness"


def lessons_dir() -> Path:
    """Cross-project institutional memory (spec section 12)."""
    return user_home() / "lessons"


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` looking for an initialised project."""
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / HARNESS_DIRNAME).is_dir():
            return candidate
    return None


def harness_dir(project_root: Path) -> Path:
    return project_root / HARNESS_DIRNAME


def overrides_dir(project_root: Path) -> Path:
    return harness_dir(project_root) / "overrides"


def resolve_content(relpath: str, project_root: Path | None = None) -> Path:
    """Resolve a ``core/``-relative path through the three tiers.

    Raises FileNotFoundError if no tier provides it — a missing agent contract
    is a real failure, not something to paper over with a default.
    """
    tiers: list[Path] = []
    if project_root is not None:
        tiers.append(overrides_dir(project_root))
    tiers.append(user_home())
    tiers.append(package_core())

    for tier in tiers:
        candidate = tier / relpath
        if candidate.exists():
            return candidate

    searched = ", ".join(str(t / relpath) for t in tiers)
    raise FileNotFoundError(f"could not resolve {relpath!r}; searched: {searched}")


class Project:
    """Paths for one initialised target project."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    @property
    def harness(self) -> Path:
        return harness_dir(self.root)

    @property
    def events(self) -> Path:
        return self.harness / "events"

    @property
    def state_file(self) -> Path:
        return self.harness / "state.json"

    @property
    def config_file(self) -> Path:
        return self.harness / "config.json"

    @property
    def revised_spec(self) -> Path:
        return self.harness / "revised-spec.md"

    @property
    def open_questions(self) -> Path:
        return self.harness / "open-questions.md"

    @property
    def tasks(self) -> Path:
        return self.harness / "tasks"

    @property
    def reports(self) -> Path:
        return self.harness / "reports"

    @property
    def architecture(self) -> Path:
        return self.harness / "architecture"

    def resolve(self, relpath: str) -> Path:
        return resolve_content(relpath, self.root)

    def ensure_layout(self) -> None:
        for d in (self.harness, self.events, self.tasks, self.reports,
                  self.architecture, overrides_dir(self.root)):
            d.mkdir(parents=True, exist_ok=True)
