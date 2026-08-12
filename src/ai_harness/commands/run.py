"""`harness run` and `harness run-ticket` — phase 3, execution.

``run-ticket`` drives one ticket through the linear pipeline; ``run`` drives
every dependency-ready ticket, each in its own worktree and its own container.

The environment setup is the interesting part, and it is shared: a ticket gets
a git worktree (spec section 11) and a container bound to it (section 10). The
container is required, because "always install, never assume" is only safe in a
sandbox and an autonomous agent running arbitrary builds should not be doing so
against the developer's machine. ``--no-container`` waives that, and the waiver
is written to the event log — a run that touched the host is not the same
evidence as one that did not, and the audit trail should say which it was.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import containers, gates, pipeline, taskfile, worktrees
from ..containers import NO_DOCKER, DockerExecutor, docker_available
from ..context_index import NO_TREE_SITTER, available as index_available
from ..execution import Executor, HostExecutor, posix_shell
from ..llm import NO_CREDENTIALS, credentials_available
from ..paths import Project
from ..pipeline import PipelineError, TicketPipeline, TicketOutcome
from ..registry import Registry
from ..state import fold
from ..taskfile import TaskFile, TaskFileError
from ..thresholds import Thresholds
from ..worktrees import GitError
from ._common import (CommandError, confirm, die, info, open_log, rel,
                      require_project, sync_state, warn)


def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "run-ticket",
        help="run one ticket through the execution pipeline",
        description="Phase 3. Architect -> Developer -> gates -> "
                    "{Security, QA, Performance} -> Reviewer -> human approval gate.",
    )
    p.add_argument("ticket", help="task id, e.g. T-001")
    _shared_arguments(p)
    p.set_defaults(func=run_ticket)

    q = subparsers.add_parser(
        "run",
        help="run every dependency-ready ticket",
        description="Phase 3. Tickets whose depends_on are all done run one after "
                    "another, each in its own worktree and container.",
    )
    q.add_argument("--limit", type=int, metavar="N",
                   help="stop after N tickets")
    _shared_arguments(q)
    q.set_defaults(func=run_all)


def _shared_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-container", action="store_true",
                   help="run on the host instead of a devcontainer, waiving the "
                        "isolation spec section 10 requires (recorded in the log)")
    p.add_argument("--keep-worktree", action="store_true",
                   help="leave the ticket worktree in place after the run")
    p.add_argument("--dry-run", action="store_true",
                   help="check preconditions and print the plan, then stop")
    p.add_argument("--yes", action="store_true",
                   help="skip the confirmation prompt")


# ------------------------------------------------------------------ setup

def _preconditions(project: Project, args) -> tuple[list[TaskFile], Registry, Thresholds]:
    state = fold(open_log(project))
    if state["spec"]["status"] != "approved":
        die("the spec is not approved yet. Run `harness review-specs --approve` first.")

    try:
        tasks = taskfile.load_all(project)
    except TaskFileError as exc:
        die(f"task files do not validate:\n{exc}")
    if not tasks:
        die("no task files. Run `harness chunk-specs` first.")

    if not worktrees.is_repo(project.root):
        die(f"{project.root} is not a git repository.\n"
            "Phase 3 runs each ticket in its own git worktree.")

    if not args.dry_run and not credentials_available():
        die(NO_CREDENTIALS)

    if not index_available():
        warn(f"the context index is unavailable, so agents will read files directly.\n"
             f"{NO_TREE_SITTER}")

    return tasks, Registry.load(project), Thresholds.load(project)


def _make_executor(project: Project, ticket: str, root: Path, args,
                   log) -> tuple[Executor, str, str]:
    """Returns the executor, the gates directory as it sees it, and a shell."""
    gates_source = project.resolve("gates")

    if args.no_container:
        shell = posix_shell()
        if shell is None:
            die("no POSIX shell found for --no-container mode; the gate scripts need "
                "one. Install Git for Windows, or drop --no-container and use Docker.")
        log.append("ticket.isolation_waived", {
            "task_id": ticket, "reason": "--no-container",
            "detail": "gates and agent commands ran directly on the host",
        }, stream=ticket)
        return HostExecutor(root=root), gates_source.as_posix(), shell

    ok, detail = docker_available()
    if not ok:
        die(f"{NO_DOCKER}\n({detail})")

    executor = DockerExecutor(root=root, ticket=ticket,
                              extra_mounts={gates_source: gates.GATES_MOUNT})
    executor.ensure_image(project.resolve("Dockerfile"))
    executor.start()
    for command in containers.bootstrap_commands(root):
        info(f"  {ticket}  bootstrap: {' '.join(command)}")
        executor.run(command)
    return executor, gates.GATES_MOUNT, "sh"


def _drive(project: Project, task: TaskFile, args, registry: Registry,
           thresholds: Thresholds) -> TicketOutcome:
    log = open_log(project)
    try:
        worktree = worktrees.ensure(project, task.id)
    except GitError as exc:
        raise CommandError(str(exc)) from exc

    log.append("ticket.worktree_created", {
        "task_id": task.id, "branch": worktree.branch,
        "path": rel(worktree.path, project.root),
    }, stream=task.id)
    info(f"  {task.id}  worktree {rel(worktree.path, project.root)} "
         f"on {worktree.branch}")

    (worktree.path / ".devcontainer").mkdir(parents=True, exist_ok=True)
    (worktree.path / ".devcontainer" / "devcontainer.json").write_text(
        containers.devcontainer_json(), encoding="utf-8")

    executor, gates_dir, shell = _make_executor(project, task.id, worktree.path, args, log)
    try:
        engine = TicketPipeline(
            project, task, log=log, executor=executor, worktree=worktree,
            registry=registry, thresholds=thresholds, gates_dir=gates_dir,
            shell=shell, announce=info)
        return engine.run()
    finally:
        executor.dispose()
        sync_state(project)
        if not args.keep_worktree:
            try:
                worktrees.remove(project, task.id)
            except GitError:
                info(f"  {task.id}  worktree kept (uncommitted changes)")


def _report(outcome: TicketOutcome, project: Project) -> None:
    info("")
    if outcome.status == "awaiting_approval":
        info(f"{outcome.ticket}: APPROVED by the Reviewer after "
             f"{outcome.cycles} cycle(s) — awaiting your approval.")
        info(f"  evidence: {rel(project.reports / outcome.ticket, project.root)}/")
        info(f"  next:     harness approve {outcome.ticket}")
    elif outcome.status == "blocked":
        info(f"{outcome.ticket}: blocked — {outcome.detail}")
        info(f"  reports:  {rel(project.reports / outcome.ticket, project.root)}/")
    else:
        info(f"{outcome.ticket}: {outcome.status} — {outcome.detail}")


# -------------------------------------------------------------------- run

def run_ticket(args: argparse.Namespace) -> int:
    project = require_project()
    tasks, registry, thresholds = _preconditions(project, args)

    task = next((t for t in tasks if t.id == args.ticket), None)
    if task is None:
        die(f"no task {args.ticket}; known: {', '.join(t.id for t in tasks)}")

    state = fold(open_log(project))
    unmet = [d for d in task.depends_on
             if (state["tasks"].get(d) or {}).get("status") not in ("done", "human_approved")]
    if unmet:
        die(f"{task.id} depends on {', '.join(unmet)}, which are not done yet.")

    _print_plan(project, [task], args)
    if args.dry_run:
        info("")
        info("--dry-run: nothing was run.")
        return 0
    if not args.yes and not confirm(f"Run {task.id}?"):
        info("Aborted.")
        return 1

    try:
        outcome = _drive(project, task, args, registry, thresholds)
    except PipelineError as exc:
        sync_state(project)
        raise CommandError(f"{task.id}: {exc}") from exc

    _report(outcome, project)
    return 0 if outcome.status == "awaiting_approval" else 1


def run_all(args: argparse.Namespace) -> int:
    project = require_project()
    tasks, registry, thresholds = _preconditions(project, args)

    state = fold(open_log(project))
    ready = pipeline.ready_tickets(state, tasks)
    if args.limit:
        ready = ready[:args.limit]
    if not ready:
        info("No tickets are ready: every pending ticket is waiting on a dependency.")
        info("Run `harness status` to see the graph.")
        return 0

    _print_plan(project, ready, args)
    if args.dry_run:
        info("")
        info("--dry-run: nothing was run.")
        return 0
    if not args.yes and not confirm(f"Run {len(ready)} ticket(s)?"):
        info("Aborted.")
        return 1

    outcomes: list[TicketOutcome] = []
    for task in ready:
        try:
            outcomes.append(_drive(project, task, args, registry, thresholds))
        except (PipelineError, CommandError) as exc:
            warn(f"{task.id}: {exc}")
            outcomes.append(TicketOutcome(task.id, "failed", detail=str(exc)))

    info("")
    info("Summary")
    for outcome in outcomes:
        _report(outcome, project)

    awaiting = [o.ticket for o in outcomes if o.status == "awaiting_approval"]
    if awaiting:
        info("")
        info(f"Awaiting your approval: {', '.join(awaiting)}")
    return 0 if awaiting and len(awaiting) == len(outcomes) else 1


def _print_plan(project: Project, tasks: list[TaskFile], args) -> None:
    isolation = "host (isolation waived)" if args.no_container else "devcontainer per ticket"
    info("")
    info("Plan  (phase 3 execution)")
    info("")
    info(f"  isolation    {isolation}")
    info(f"  worktrees    {rel(worktrees.worktrees_root(project), project.root)}/<ticket>")
    info(f"  tickets      {len(tasks)}")
    for task in tasks:
        deps = f"  after {', '.join(task.depends_on)}" if task.depends_on else ""
        info(f"    {task.id}  [{task.dir}]  {task.frontmatter['title']}{deps}")
    info("")
    info("  Per ticket: architect -> developer -> gates -> "
         "{security, qa, performance} -> reviewer")
    info("  Nothing merges or closes without your approval.")
