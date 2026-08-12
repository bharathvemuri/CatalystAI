"""`harness chunk-specs` — phase 1, chunking.

Turns the approved spec into one task file per unit of work, with explicit
``depends_on`` edges. Those edges are what let phase 3 run tickets concurrently
without collisions, so they are validated here rather than trusted.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from .. import contracts, loaders
from ..contracts import ContractViolation
from ..llm import NO_CREDENTIALS, LLM, LLMError, credentials_available
from ..paths import Project
from ..registry import Registry
from ..state import fold
from ._common import (CommandError, die, info, open_log, read_config, rel,
                      require_project, sync_state)

CHUNK_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "phase": {"type": "integer"},
                    "dir": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "start_condition": {"type": "string"},
                    "done_condition": {"type": "string"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "context": {"type": "string"},
                    "task": {"type": "string"},
                },
                "required": ["id", "title", "phase", "dir", "depends_on", "inputs",
                             "start_condition", "done_condition",
                             "acceptance_criteria", "context", "task"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["tasks", "notes"],
    "additionalProperties": False,
}

FRONTMATTER_KEYS = ["id", "title", "phase", "dir", "depends_on", "inputs",
                    "start_condition", "done_condition", "acceptance_criteria", "status"]


def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "chunk-specs",
        help="split the approved spec into dependency-ordered task files",
        description="Requires an approved spec. Writes .harness/tasks/<dir>/T-XXX.md.",
    )
    p.add_argument("--model", metavar="ID", help="override the registry default")
    p.add_argument("--force", action="store_true",
                   help="overwrite existing task files")
    p.add_argument("--dry-run", action="store_true",
                   help="validate and print the plan without writing anything")
    p.set_defaults(func=run)


def _validate_graph(tasks: list[dict[str, Any]]) -> None:
    ids = [t["id"] for t in tasks]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        die(f"duplicate task ids: {', '.join(sorted(duplicates))}")

    known = set(ids)
    problems: list[str] = []
    for task in tasks:
        for dep in task["depends_on"]:
            if dep not in known:
                problems.append(f"{task['id']} depends on unknown task {dep}")
            if dep == task["id"]:
                problems.append(f"{task['id']} depends on itself")
    if problems:
        die("dependency graph is invalid:\n" + "\n".join(f"  - {p}" for p in problems))

    # Kahn's algorithm; whatever is left when no node has zero in-degree is a cycle.
    remaining = {t["id"]: set(t["depends_on"]) for t in tasks}
    while True:
        ready = [tid for tid, deps in remaining.items() if not deps]
        if not ready:
            break
        for tid in ready:
            remaining.pop(tid)
            for deps in remaining.values():
                deps.discard(tid)
    if remaining:
        die("dependency cycle among: " + ", ".join(sorted(remaining)))


def _render_task_file(task: dict[str, Any]) -> str:
    frontmatter = {k: task[k] for k in FRONTMATTER_KEYS if k in task}
    yaml_block = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True,
                                default_flow_style=False).rstrip()
    return (
        f"---\n{yaml_block}\n---\n\n"
        f"## Context\n\n{task['context'].strip()}\n\n"
        f"## Task\n\n{task['task'].strip()}\n\n"
        f"## Acceptance criteria\n\n"
        + "\n".join(f"- [ ] {c.strip()}" for c in task["acceptance_criteria"])
        + "\n"
    )


def _existing_task_files(project: Project) -> list[Path]:
    return sorted(project.tasks.rglob("T-*.md"))


def run(args: argparse.Namespace) -> int:
    project = require_project()
    log = open_log(project)
    state = fold(log)

    if state["spec"]["status"] != "approved":
        die("the spec is not approved yet.\n"
            "Run `harness review-specs` until it comes back clean, then "
            "`harness review-specs --approve`.")

    if not project.revised_spec.exists():
        die(f"{rel(project.revised_spec, project.root)} does not exist — there is "
            "no Q&A log to chunk from.")

    existing = _existing_task_files(project)
    if existing and not (args.force or args.dry_run):
        die(f"{len(existing)} task file(s) already exist under "
            f"{rel(project.tasks, project.root)}. Re-run with --force to replace them.")

    config = read_config(project)
    docs_block = ""
    if config.get("docs_dir"):
        docs_dir = (project.root / config["docs_dir"]).resolve()
        if docs_dir.is_dir():
            docs, _ = loaders.load_dir(docs_dir)
            docs_block = loaders.as_prompt_block(docs, docs_dir)

    registry = Registry.load(project)
    model = registry.validate(args.model) if args.model else registry.default_for("architect")
    if not credentials_available():
        die(NO_CREDENTIALS)
    llm = LLM(model=model, effort=registry.effort_for("architect"), registry=registry)

    system = project.resolve("prompts/chunk-specs.md").read_text(encoding="utf-8")
    user_parts = []
    if docs_block:
        user_parts += ["<source_documents>", docs_block, "</source_documents>", ""]
    user_parts += [
        "<answered_questions>",
        "The authoritative Q&A log. Where it conflicts with the source documents, "
        "these answers win.",
        "",
        project.revised_spec.read_text(encoding="utf-8"),
        "</answered_questions>",
        "",
        "Produce the task breakdown.",
    ]

    info(f"Chunking with {model} (effort={registry.effort_for('architect')})…")
    try:
        result = llm.structured(system=system, user="\n".join(user_parts),
                                schema=CHUNK_SCHEMA, max_tokens=32000)
    except LLMError as exc:
        raise CommandError(str(exc)) from exc

    tasks = result.get("tasks", [])
    if not tasks:
        die("the chunker returned no tasks. That usually means the spec is too "
            "thin to implement from — re-run `harness review-specs`.")

    for task in tasks:
        task["status"] = "pending"

    # Contract first, then the graph. A task that fails its schema never reaches disk.
    for task in tasks:
        frontmatter = {k: task[k] for k in FRONTMATTER_KEYS if k in task}
        try:
            contracts.validate("task", frontmatter, project)
        except ContractViolation as exc:
            raise CommandError(f"task {task.get('id', '?')} failed its contract:\n{exc}") from exc
    _validate_graph(tasks)

    by_dir: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        by_dir.setdefault(task["dir"], []).append(task)

    info("")
    info(f"{len(tasks)} task(s) across {len(by_dir)} director(ies):")
    for directory in sorted(by_dir):
        info(f"  {directory}/")
        for task in by_dir[directory]:
            deps = f"  ← {', '.join(task['depends_on'])}" if task["depends_on"] else ""
            info(f"    {task['id']}  phase {task['phase']}  {task['title']}{deps}")

    if result.get("notes"):
        info("")
        info(result["notes"])

    if args.dry_run:
        info("")
        info("--dry-run: nothing written.")
        return 0

    if args.force:
        for path in existing:
            path.unlink()

    written: list[Path] = []
    for task in tasks:
        target_dir = project.tasks / task["dir"]
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{task['id']}.md"
        path.write_text(_render_task_file(task), encoding="utf-8")
        written.append(path)

    log.append("tasks.generated", {
        "tasks": [{"id": t["id"], "dir": t["dir"], "phase": t["phase"],
                   "title": t["title"], "depends_on": t["depends_on"]}
                  for t in tasks],
        "notes": result.get("notes", ""),
    })
    state = sync_state(project)

    info("")
    info(f"Wrote {len(written)} task file(s) under {rel(project.tasks, project.root)}")
    info(f"phase: {state['phase']}")
    info("")
    info("Next: harness status   (phase 2 setup is not implemented yet)")
    return 0
