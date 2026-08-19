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
import os
from pathlib import Path

from .. import containers, env, gates, pipeline, runner as runners, taskfile, worktrees
from ..containers import NO_DOCKER, DockerExecutor, docker_available
from ..context_index import NO_TREE_SITTER, available as index_available
from ..execution import Executor, HostExecutor, posix_shell
from ..paths import Project
from ..pipeline import PipelineError, TicketPipeline, TicketOutcome
from ..registry import Registry
from ..runner import RunnerUnavailable
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
    runners.add_argument(p)


# ------------------------------------------------------------------ setup

def preconditions(project: Project, args
                  ) -> tuple[list[TaskFile], Registry, Thresholds, str]:
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

    # Credentials the SDK and the API runner's pre-flight check read from the
    # environment are documented to live in `.env` too (see `.env.example`), so
    # load those tiers before selecting a backend — otherwise a key sitting in
    # the repo's `.env` is invisible and the run falls back to the CLI login.
    loaded = env.load_into_environ(project)
    for name, source in loaded.items():
        info(f"  credential   {name} from {rel(Path(source), project.root)}")

    # A dry run still resolves the backend, because "which model calls would
    # this make, billed to what" is exactly what a plan is for. It just does
    # not fail the command when none is configured.
    try:
        backend = runners.select(getattr(args, "runner", None))
    except RunnerUnavailable as exc:
        if not args.dry_run:
            die(str(exc))
        backend = ""

    if backend == runners.CLAUDE_CODE and loaded:
        # The keys above were loaded only so the API runner could be *considered*.
        # The Claude Code Agent SDK prefers ANTHROPIC_API_KEY over the claude.ai
        # login, so a `.env` key left set would route claude-code through the API
        # (and its billing) instead of the subscription. Undo the load for this
        # backend; a key exported in the real environment is left untouched.
        for name in loaded:
            os.environ.pop(name, None)

    if not index_available():
        warn(f"the context index is unavailable, so agents will read files directly.\n"
             f"{NO_TREE_SITTER}")

    return tasks, Registry.load(project), Thresholds.load(project), backend


def make_executor(project: Project, ticket: str, root: Path, args,
                  log) -> tuple[Executor, str, str, str]:
    """Returns the executor, the gates directory and report directory as it
    sees them, and a shell.

    The two directories are returned rather than derived by the caller because
    they name the same places from different sides of a mount: on the host they
    are host paths, and inside a container they are mount points.

    Shared with the phase 4 documentation pass, which needs the same isolation
    and the same waiver record for exactly the same reasons.
    """
    gates_source = project.resolve("gates")
    reports_source = project.reports / ticket
    # The gate scripts write into it, so it has to exist before the mount: Docker
    # would otherwise create it as a root-owned directory on the host.
    reports_source.mkdir(parents=True, exist_ok=True)

    if args.no_container:
        shell = posix_shell()
        if shell is None:
            die("no POSIX shell found for --no-container mode; the gate scripts need "
                "one. Install Git for Windows, or drop --no-container and use Docker.")
        log.append("ticket.isolation_waived", {
            "task_id": ticket, "reason": "--no-container",
            "detail": "gates and agent commands ran directly on the host",
        }, stream=ticket)
        return (HostExecutor(root=root), gates_source.as_posix(),
                reports_source.as_posix(), shell)

    ok, detail = docker_available()
    if not ok:
        die(f"{NO_DOCKER}\n({detail})")

    executor = DockerExecutor(root=root, ticket=ticket, extra_mounts={
        gates_source: (gates.GATES_MOUNT, "ro"),
        reports_source: (containers.REPORTS_MOUNT, "rw"),
    })
    # This runs before the caller can wrap the executor in its own try/finally,
    # so a failure here — bootstrap is the usual one — has to clean up its own
    # container. A leaked container outlives the run and, being named per
    # (project, ticket), is what the next run reattaches to.
    try:
        executor.ensure_image(project.resolve("Dockerfile"))
        executor.start()
        _bootstrap(executor, ticket, root)
    except BaseException:
        executor.dispose()
        raise
    return executor, gates.GATES_MOUNT, containers.REPORTS_MOUNT, "sh"


def _bootstrap(executor: Executor, ticket: str, root: Path) -> None:
    """Install what the repository declares, and stop if that fails.

    A discarded bootstrap failure is worse than a loud one: the build gate fails
    later for a reason that has nothing to do with the code, and the evidence
    the Reviewer reads describes a symptom rather than the cause.
    """
    for command in containers.bootstrap_commands(root):
        info(f"  {ticket}  bootstrap: {' '.join(command)}")
        outcome = executor.run(command)
        if not outcome.ok:
            tail = ((outcome.stderr or "") + (outcome.stdout or "")).strip()[-2000:]
            die(f"{ticket}: bootstrap command {' '.join(command)!r} failed with "
                f"exit {outcome.exit_code}. The container's environment is not "
                f"what the repository declares, so nothing after this would mean "
                f"anything.\n\n{tail}")


def _drive(project: Project, task: TaskFile, args, registry: Registry,
           thresholds: Thresholds, backend: str) -> TicketOutcome:
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
        containers.devcontainer_json(containers.image_tag(project.resolve("Dockerfile"))),
        encoding="utf-8")

    executor, gates_dir, report_dir, shell = make_executor(
        project, task.id, worktree.path, args, log)
    try:
        engine = TicketPipeline(
            project, task, log=log, executor=executor, worktree=worktree,
            registry=registry, thresholds=thresholds, gates_dir=gates_dir,
            report_dir=report_dir, shell=shell, backend=backend, announce=info)
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
    tasks, registry, thresholds, backend = preconditions(project, args)

    task = next((t for t in tasks if t.id == args.ticket), None)
    if task is None:
        die(f"no task {args.ticket}; known: {', '.join(t.id for t in tasks)}")

    state = fold(open_log(project))
    unmet = [d for d in task.depends_on
             if (state["tasks"].get(d) or {}).get("status") not in ("done", "human_approved")]
    if unmet:
        die(f"{task.id} depends on {', '.join(unmet)}, which are not done yet.")

    _print_plan(project, [task], args, backend)
    if args.dry_run:
        info("")
        info("--dry-run: nothing was run.")
        return 0
    if not args.yes and not confirm(f"Run {task.id}?"):
        info("Aborted.")
        return 1

    try:
        outcome = _drive(project, task, args, registry, thresholds, backend)
    except PipelineError as exc:
        sync_state(project)
        raise CommandError(f"{task.id}: {exc}") from exc

    _report(outcome, project)
    return 0 if outcome.status == "awaiting_approval" else 1


def run_all(args: argparse.Namespace) -> int:
    project = require_project()
    tasks, registry, thresholds, backend = preconditions(project, args)

    state = fold(open_log(project))
    ready = pipeline.ready_tickets(state, tasks)
    if args.limit:
        ready = ready[:args.limit]
    if not ready:
        info("No tickets are ready: every pending ticket is waiting on a dependency.")
        info("Run `harness status` to see the graph.")
        return 0

    _print_plan(project, ready, args, backend)
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
            outcomes.append(_drive(project, task, args, registry, thresholds, backend))
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


def _print_plan(project: Project, tasks: list[TaskFile], args, backend: str) -> None:
    isolation = "host (isolation waived)" if args.no_container else "devcontainer per ticket"
    info("")
    info("Plan  (phase 3 execution)")
    info("")
    info(f"  runner       {runners.describe(backend) if backend else 'none available'}")
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
