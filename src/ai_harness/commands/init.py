"""`harness init` — attach the harness to a target repository."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import HARNESS_VERSION
from ..events import EventLog, utcnow
from ..paths import Project, find_project_root, lessons_dir, user_home
from ._common import die, info, read_config, rel, sync_state, write_config

GITIGNORE_HINT = """\
# Harness working directory. The event log and revised-spec.md are the audit
# trail and belong in version control; the derived view and generated index do not.
.harness/state.json
.harness/index/
.harness/worktrees/
"""


def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "init",
        help="initialise .harness/ in a target repository",
        description="Create the harness working directory for a target project. "
                    "Non-destructive: it never touches your source tree.",
    )
    p.add_argument("path", nargs="?", default=".",
                   help="target repository root (default: current directory)")
    p.add_argument("--docs", metavar="DIR",
                   help="folder holding the source specification documents")
    p.add_argument("--force", action="store_true",
                   help="re-initialise even if .harness/ already exists")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        die(f"{root} is not a directory")

    existing = find_project_root(root)
    if existing == root and not args.force:
        die(f"{rel(root / '.harness', root)} already exists. "
            "Nothing to do — pass --force to re-initialise.")
    if existing is not None and existing != root:
        info(f"note: a parent project already exists at {existing}")

    project = Project(root)
    project.ensure_layout()
    user_home().mkdir(parents=True, exist_ok=True)
    lessons_dir().mkdir(parents=True, exist_ok=True)

    config = read_config(project)
    config.update({
        "harness_version": HARNESS_VERSION,
        "initialized_at": config.get("initialized_at", utcnow()),
    })
    if args.docs:
        docs = Path(args.docs).expanduser()
        docs = docs if docs.is_absolute() else (root / docs)
        if not docs.is_dir():
            die(f"--docs {docs} is not a directory")
        config["docs_dir"] = rel(docs.resolve(), root)
    write_config(project, config)

    log = EventLog(project.events)
    log.append("harness.initialized", {
        "harness_version": HARNESS_VERSION,
        "root": str(root),
        "docs_dir": config.get("docs_dir"),
    })
    state = sync_state(project)

    hint = project.harness / "gitignore-suggestions"
    hint.write_text(GITIGNORE_HINT, encoding="utf-8")

    info(f"Initialised harness in {root}")
    info("")
    info(f"  event log     {rel(project.events, root)}/       (source of truth, append-only)")
    info(f"  derived state {rel(project.state_file, root)}     (read-only, regenerated)")
    info(f"  overrides     {rel(project.harness / 'overrides', root)}/    (per-project agent/gate customisation)")
    info(f"  lessons       {lessons_dir()}   (cross-project, spec section 12)")
    info("")
    info(f"  phase: {state['phase']}   spec: {state['spec']['status']}")
    info("")
    if config.get("docs_dir"):
        info(f"Next: harness review-specs   (reads {config['docs_dir']}/)")
    else:
        info("Next: harness review-specs --docs <folder-with-your-spec-documents>")
    return 0
