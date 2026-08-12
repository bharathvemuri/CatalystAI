"""Tracker adapter registry.

Adding Jira means writing one module here and one line in ``_BUILDERS`` — the
command layer names a provider string and never learns what is behind it.
"""

from __future__ import annotations

from typing import Callable

from ..paths import Project
from .base import (Issue, IssueSpec, RepoRef, Tracker, TrackerError,
                   TrackerItemError, TrackerSystemicError)

__all__ = ["Issue", "IssueSpec", "RepoRef", "Tracker", "TrackerError",
           "TrackerItemError", "TrackerSystemicError", "get", "providers"]

DEFAULT_PROVIDER = "github"


def _github(project: Project | None) -> tuple[Tracker, str]:
    from .github import GitHubTracker, resolve_token
    token, source = resolve_token(project)
    return GitHubTracker(token), source


_BUILDERS: dict[str, Callable[[Project | None], tuple[Tracker, str]]] = {
    "github": _github,
}


def providers() -> list[str]:
    return sorted(_BUILDERS)


def get(provider: str, project: Project | None = None) -> tuple[Tracker, str]:
    """Build a tracker. Returns it with a description of the credential source."""
    if provider not in _BUILDERS:
        raise TrackerError(
            f"unknown tracker provider {provider!r}; available: {', '.join(providers())}")
    return _BUILDERS[provider](project)
