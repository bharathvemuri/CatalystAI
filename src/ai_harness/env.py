"""Credential resolution for outward-facing adapters.

Spec section 4 puts tracker credentials in a gitignored ``.env`` in the target
repo. On its own that forces the same token to be re-entered for every project
the harness is pointed at, so resolution is layered the same way content is
(``paths.resolve_content``): the process environment wins, then the target
repo's ``.env``, then the user-level one. A project can override; a user who
never needs to override sets the token once.

Callers get back the value *and where it came from*, because the plan a user
approves should name the source of the credential it is about to act with. The
value itself is returned to the caller and nowhere else — never logged, never
printed, never written to state.
"""

from __future__ import annotations

import os
from pathlib import Path

from .paths import Project, user_home

DOTENV_NAME = ".env"


def parse(text: str) -> dict[str, str]:
    """Parse the ``KEY=value`` subset of dotenv syntax that credentials need."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:  # trailing comment on an unquoted value
            value = value.split(" #", 1)[0].rstrip()
        values[key.strip()] = value
    return values


def files(project: Project | None = None) -> list[Path]:
    """Candidate dotenv files, most specific first."""
    candidates: list[Path] = []
    if project is not None:
        candidates.append(project.root / DOTENV_NAME)
    candidates.append(user_home() / DOTENV_NAME)
    return candidates


def lookup(name: str, project: Project | None = None) -> tuple[str, str] | None:
    """Return ``(value, source)`` for ``name``, or None if no tier provides it."""
    from_env = os.environ.get(name)
    if from_env:
        return from_env, "environment"

    for path in files(project):
        if not path.is_file():
            continue
        value = parse(path.read_text(encoding="utf-8")).get(name)
        if value:
            return value, str(path)
    return None


def describe_sources(project: Project | None = None) -> str:
    """Where a missing credential could have been put, in precedence order."""
    return " > ".join(["the environment", *(str(p) for p in files(project))])


# Credentials the Anthropic SDK and the API runner's pre-flight check read
# straight from ``os.environ`` rather than through ``lookup`` — the SDK client
# resolves them itself, and ``credentials_available`` cannot take a project.
# ``.env.example`` documents the ``.env`` tiers as their home, so they are copied
# into the environment once, at command startup, before a backend is selected.
ENVIRON_CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def load_into_environ(project: Project | None = None,
                      names: tuple[str, ...] = ENVIRON_CREDENTIALS) -> dict[str, str]:
    """Copy credential variables from the ``.env`` tiers into ``os.environ``.

    A variable already set in the real environment is left untouched, which
    keeps the precedence ``lookup`` gives — process environment over ``.env``.
    Returns ``{name: source_path}`` for those loaded from a file, so a caller can
    name the source without ever handling the value.
    """
    loaded: dict[str, str] = {}
    for name in names:
        if os.environ.get(name):
            continue
        for path in files(project):
            if not path.is_file():
                continue
            value = parse(path.read_text(encoding="utf-8")).get(name)
            if value:
                os.environ[name] = value
                loaded[name] = str(path)
                break
    return loaded
