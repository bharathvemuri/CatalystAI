"""Reading task files back off disk.

``chunk-specs`` writes these files once; every phase after it reads them, so the
frontmatter is a contract in both directions and is re-validated on load rather
than trusted because the harness happened to write it — a task file is an
ordinary markdown file a user can and will edit by hand.

Rewrites preserve the body verbatim and re-render the frontmatter in a fixed key
order, so writing an issue reference back into a file produces a one-line diff
instead of a reordered block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import contracts
from .contracts import ContractViolation
from .paths import Project

FRONTMATTER_KEYS = [
    "id", "title", "phase", "dir", "depends_on", "inputs",
    "start_condition", "done_condition", "acceptance_criteria", "status",
    "issue_ref",
]

_SPLIT = re.compile(r"^---[ \t]*\n(?P<yaml>.*?)\n---[ \t]*\n?(?P<body>.*)\Z", re.DOTALL)
_SECTION = re.compile(r"^##[ \t]+(?P<name>.+?)[ \t]*$", re.MULTILINE)


class TaskFileError(Exception):
    """A task file that cannot be read as one. Never papered over with a default."""


@dataclass
class TaskFile:
    path: Path
    frontmatter: dict[str, Any]
    body: str

    @property
    def id(self) -> str:
        return self.frontmatter["id"]

    @property
    def dir(self) -> str:
        return self.frontmatter["dir"]

    @property
    def phase(self) -> int:
        return self.frontmatter["phase"]

    @property
    def depends_on(self) -> list[str]:
        return list(self.frontmatter.get("depends_on") or [])

    @property
    def issue_ref(self) -> str | None:
        return self.frontmatter.get("issue_ref")

    def section(self, name: str) -> str:
        """Body text under a ``## <name>`` heading, or '' if there is none."""
        matches = list(_SECTION.finditer(self.body))
        for index, match in enumerate(matches):
            if match.group("name").strip().lower() != name.strip().lower():
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(self.body)
            return self.body[match.end():end].strip()
        return ""

    def render(self) -> str:
        ordered = {k: self.frontmatter[k] for k in FRONTMATTER_KEYS
                   if k in self.frontmatter}
        ordered.update({k: v for k, v in self.frontmatter.items()
                        if k not in FRONTMATTER_KEYS})
        block = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True,
                               default_flow_style=False).rstrip()
        return f"---\n{block}\n---\n\n{self.body.lstrip()}"

    def save(self) -> None:
        self.path.write_text(self.render(), encoding="utf-8")


def split(text: str) -> tuple[dict[str, Any], str]:
    match = _SPLIT.match(text)
    if not match:
        raise TaskFileError("no YAML frontmatter block (expected a leading '---' fence)")
    try:
        frontmatter = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError as exc:
        raise TaskFileError(f"frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise TaskFileError("frontmatter is not a mapping")
    return frontmatter, match.group("body")


def load(path: Path, project: Project | None = None) -> TaskFile:
    """Read and validate one task file."""
    try:
        frontmatter, body = split(path.read_text(encoding="utf-8"))
    except TaskFileError as exc:
        raise TaskFileError(f"{path.name}: {exc}") from exc

    try:
        contracts.validate("task", frontmatter, project)
    except ContractViolation as exc:
        raise TaskFileError(f"{path.name} does not satisfy the task contract:\n{exc}") from exc

    return TaskFile(path=path, frontmatter=frontmatter, body=body)


def paths(project: Project) -> list[Path]:
    return sorted(project.tasks.rglob("T-*.md"))


def load_all(project: Project) -> list[TaskFile]:
    """Every task file, id-ordered. One bad file fails the whole load."""
    problems: list[str] = []
    tasks: list[TaskFile] = []
    for path in paths(project):
        try:
            tasks.append(load(path, project))
        except TaskFileError as exc:
            problems.append(str(exc))
    if problems:
        raise TaskFileError("\n".join(problems))
    return sorted(tasks, key=lambda t: t.id)
