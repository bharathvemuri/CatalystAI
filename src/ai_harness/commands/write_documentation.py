"""`harness write-documentation` — phase 4, documentation.

The project-level pass. Phase 3 already runs a Documentation agent per ticket,
which keeps docs from drifting as each ticket lands; this is what reconciles a
dozen ticket-scoped edits into one coherent set and guarantees the three
top-level documents exist.

Spec section 6 asks for two things that pull against each other: *always*
produce README, ARCHITECTURE and CONTRIBUTING, but *only* touch documentation
the implementation actually affected. Resolved by treating existence and content
as separate obligations — the three files are guaranteed to exist, and their
contents change only where a shipped ticket drove the change. Which of the three
were considered and deliberately left alone is recorded, so "only what was
affected" is checkable rather than asserted.

Two checks are deliberately mechanical, because the agent's own account of its
work is exactly the thing that should not be taken on trust:

- The three required files are looked for **on disk** after the pass, not read
  out of the agent's report.
- What the agent says it touched is compared against what git says changed.
  Either direction of mismatch is recorded as a contract violation.

And one thing is checked before any of it: a ticket's work lives on its own
branch until its pull request merges, so a pass whose base does not contain a
shipped ticket's commits cannot see that ticket's code. That is reported up
front rather than discovered in documentation that describes code which is not
there.
"""

from __future__ import annotations

import argparse

from .. import agents, containers, trackers, worktrees
from ..agents import AgentError, AgentResult
from ..context_index import ContextIndex, ContextIndexUnavailable
from ..paths import Project
from ..registry import Registry
from ..state import fold
from ..taskfile import TaskFile
from ..trackers import TrackerError
from ..trackers.github import parse_repo_arg
from ..worktrees import GitError
from ._common import (CommandError, confirm, die, info, open_log, rel,
                      require_project, sync_state, warn)
from .run import make_executor, preconditions

DOCS_NAME = "docs"
# The report lives under .harness/reports/documentation/, beside the per-ticket
# report directories rather than inside one of them.
REPORT_DIR = "documentation"
REQUIRED_DOCS = ("README.md", "ARCHITECTURE.md", "CONTRIBUTING.md")
SHIPPED_STATUSES = ("human_approved", "done")
MAX_REPORT_CHARS = 8000

# The harness writes these into the worktree itself before the agent starts.
# They are not the agent's work and must not be attributed to it, or every run
# reports a violation for a file the agent never saw.
HARNESS_AUTHORED = (".devcontainer",)


def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "write-documentation",
        help="document what shipped, across the whole project",
        description="Phase 4. Reads every shipped ticket and its reports, then "
                    "produces README.md, ARCHITECTURE.md, CONTRIBUTING.md and "
                    "whatever else this project's type warrants. Runs in its own "
                    "worktree; nothing is pushed without your confirmation.",
    )
    p.add_argument("--base", metavar="REF",
                   help="ref to document and branch from (default: current HEAD)")
    p.add_argument("--no-container", action="store_true",
                   help="run on the host instead of a devcontainer, waiving the "
                        "isolation spec section 10 requires (recorded in the log)")
    p.add_argument("--keep-worktree", action="store_true",
                   help="leave the docs worktree in place after the run")
    p.add_argument("--no-pr", action="store_true",
                   help="commit the branch but never offer a pull request")
    p.add_argument("--dry-run", action="store_true",
                   help="check preconditions and print the plan, then stop")
    p.add_argument("--yes", action="store_true",
                   help="skip the confirmation prompt")
    p.set_defaults(func=run)


# ------------------------------------------------------------------- pieces

def shipped_tickets(state: dict, tasks: list[TaskFile]) -> list[TaskFile]:
    """Only what a human approved or that is done. Anything else has not shipped."""
    status = {tid: entry.get("status") for tid, entry in state.get("tasks", {}).items()}
    return [t for t in tasks if status.get(t.id) in SHIPPED_STATUSES]


def _read_report(project: Project, ticket: str, agent: str) -> str:
    path = project.reports / ticket / agents.REPORT_FILES[agent]
    if not path.is_file():
        return ""
    body = path.read_text(encoding="utf-8")
    if len(body) > MAX_REPORT_CHARS:
        return body[:MAX_REPORT_CHARS] + "\n[truncated]"
    return body


def _evidence(project: Project, shipped: list[TaskFile]) -> str:
    """Everything the pass is allowed to document, assembled from disk."""
    parts: list[str] = []
    for task in shipped:
        parts += [
            f"<shipped_ticket id=\"{task.id}\" dir=\"{task.dir}\" phase=\"{task.phase}\">",
            task.path.read_text(encoding="utf-8"),
            "",
            "<implementation_plan>",
            _read_report(project, task.id, "architect") or "(none)",
            "</implementation_plan>",
            "",
            "<implementation_report>",
            _read_report(project, task.id, "developer") or "(none)",
            "</implementation_report>",
            "",
            "<ticket_documentation_report>",
            _read_report(project, task.id, "documentation") or "(none)",
            "</ticket_documentation_report>",
            "</shipped_ticket>",
            "",
        ]

    adrs = sorted(project.architecture.glob("ADR-*.md")) if project.architecture.is_dir() else []
    if adrs:
        parts.append("<architecture_decisions>")
        for path in adrs:
            parts += [f"--- {path.name} ---", path.read_text(encoding="utf-8"), ""]
        parts.append("</architecture_decisions>")

    return "\n".join(parts)


def _unmerged(project: Project, shipped: list[TaskFile], base: str) -> list[str]:
    """Shipped tickets whose commits are demonstrably not reachable from the base.

    A ticket whose branch is gone is not reported: the usual reason a branch
    disappears is that its pull request merged and the branch was deleted, and a
    warning that fires on the normal path is a warning nobody reads.
    """
    unmerged = []
    for task in shipped:
        branch = worktrees.branch_for(task.id)
        if not worktrees.branch_exists(project.root, branch):
            continue
        if not worktrees.is_merged(project.root, branch, base):
            unmerged.append(task.id)
    return unmerged


def _missing_required(root) -> list[str]:
    return [name for name in REQUIRED_DOCS if not (root / name).is_file()]


def _harness_authored(path: str) -> bool:
    normalised = path.replace("\\", "/").rstrip("/")
    return any(normalised == prefix or normalised.startswith(prefix + "/")
               for prefix in HARNESS_AUTHORED)


def _verify(result: AgentResult, worktree, base: str) -> tuple[list[str], list[str]]:
    """Compare the agent's account against git, and check the three files exist.

    Returns (violations, changed_files).
    """
    violations: list[str] = []

    missing = _missing_required(worktree.path)
    if missing:
        violations.append(
            "required document(s) absent after the pass: " + ", ".join(missing))

    changed = {path for path in worktrees.changed_files(worktree, base)
               if not _harness_authored(path)}
    claimed = {entry["path"].replace("\\", "/")
               for entry in result.result.get("files_touched", [])}

    undeclared = sorted(changed - claimed)
    if undeclared:
        violations.append("changed but not declared in the report: "
                          + ", ".join(undeclared))

    unwritten = sorted(claimed - changed)
    if unwritten:
        violations.append("declared as touched but unchanged on disk: "
                          + ", ".join(unwritten))

    return violations, sorted(changed)


def _print_plan(project: Project, shipped: list[TaskFile], outstanding: list[TaskFile],
                unmerged: list[str], base: str, args) -> None:
    isolation = "host (isolation waived)" if args.no_container else "devcontainer"
    missing = _missing_required(project.root)

    info("")
    info("Plan  (phase 4 documentation)")
    info("")
    info(f"  isolation    {isolation}")
    info(f"  base         {base}")
    info(f"  worktree     {rel(worktrees.path_for(project, DOCS_NAME), project.root)} "
         f"on {worktrees.branch_for(DOCS_NAME)}")
    info(f"  shipped      {len(shipped)} ticket(s) will be documented")
    for task in shipped:
        flag = "  NOT MERGED INTO BASE" if task.id in unmerged else ""
        info(f"    {task.id}  [{task.dir}]  {task.frontmatter['title']}{flag}")
    if outstanding:
        info(f"  withheld     {len(outstanding)} ticket(s) have not shipped and will "
             f"not be documented: {', '.join(t.id for t in outstanding)}")
    info("")
    info(f"  required     {', '.join(REQUIRED_DOCS)}")
    info(f"               {('missing: ' + ', '.join(missing)) if missing else 'all present on base'}")

    if unmerged:
        info("")
        info(f"  WARNING: {len(unmerged)} shipped ticket(s) are not merged into {base}.")
        info("  Their code is not visible to this pass, so anything written about")
        info("  them would come from the reports rather than from the implementation.")
        info("  Merge their pull requests first, or pass --base with a ref that has them.")


# ---------------------------------------------------------------------- run

def run(args: argparse.Namespace) -> int:
    project = require_project()
    tasks, registry, _thresholds = preconditions(project, args)

    log = open_log(project)
    state = fold(log)

    shipped = shipped_tickets(state, tasks)
    outstanding = [t for t in tasks if t not in shipped]
    if not shipped:
        die("nothing has shipped yet, so there is nothing to document.\n"
            "Phase 4 documents tickets a human approved or that are done. "
            "Run `harness run` and `harness approve <ticket>` first.")

    try:
        base = args.base or worktrees.head_ref(project.root)
        unmerged = _unmerged(project, shipped, base)
    except GitError as exc:
        raise CommandError(str(exc)) from exc

    _print_plan(project, shipped, outstanding, unmerged, base, args)

    if args.dry_run:
        info("")
        info("--dry-run: nothing was run.")
        return 0
    if not args.yes and not confirm(f"Document {len(shipped)} shipped ticket(s)?"):
        info("Aborted.")
        return 1

    return _drive(project, shipped, base, unmerged, registry, args)


def _drive(project: Project, shipped: list[TaskFile], base: str,
           unmerged: list[str], registry: Registry, args) -> int:
    log = open_log(project)
    try:
        worktree = worktrees.ensure(project, DOCS_NAME, base=base)
    except GitError as exc:
        raise CommandError(str(exc)) from exc

    log.append("ticket.worktree_created", {
        "task_id": DOCS_NAME, "branch": worktree.branch,
        "path": rel(worktree.path, project.root), "base": base,
    }, stream=DOCS_NAME)
    info("")
    info(f"  docs  worktree {rel(worktree.path, project.root)} on {worktree.branch}")

    (worktree.path / ".devcontainer").mkdir(parents=True, exist_ok=True)
    (worktree.path / ".devcontainer" / "devcontainer.json").write_text(
        containers.devcontainer_json(), encoding="utf-8")

    executor, _gates_dir, shell = make_executor(project, DOCS_NAME, worktree.path, args, log)

    index = None
    try:
        built = ContextIndex(worktree.path)
        stats = built.build()
        built.save(project)
        index = built
        info(f"  docs  index: {stats['indexed']} file(s) parsed, {stats['reused']} reused")
    except ContextIndexUnavailable as exc:
        info(f"  docs  context index unavailable: {exc}")

    try:
        info("  docs  documentation (project pass) ...")
        result = agents.run(
            "documentation", role="documentation-project",
            project=project, root=worktree.path, executor=executor,
            user_prompt=_prompt(project, shipped, base, unmerged, worktree),
            registry=registry, index=index, shell=shell, max_turns=80,
        )
    except AgentError as exc:
        raise CommandError(str(exc)) from exc
    finally:
        executor.dispose()

    report_path = agents.write_report(project, REPORT_DIR, "documentation", result)

    if result.blocked:
        info("")
        info(f"The documentation pass stopped: {result.blocking_reason}")
        info(f"  report: {rel(report_path, project.root)}")
        return 1

    violations, changed = _verify(result, worktree, base)
    violations += result.violations

    sha = worktrees.commit_all(worktree, "docs: document what shipped")
    if sha:
        log.append("ticket.committed", {"task_id": DOCS_NAME, "sha": sha},
                   stream=DOCS_NAME)

    log.append("docs.pass_completed", {
        "base": base,
        "tickets_documented": [t.id for t in shipped],
        "files_touched": changed,
        "required_present": not _missing_required(worktree.path),
        "project_type": result.result.get("project_type", ""),
        "inferred_docs": [d["path"] for d in result.result.get("inferred_docs", [])],
        "unmerged_at_run": unmerged,
        "violations": violations,
        "model": result.model,
    }, stream=DOCS_NAME)

    state = fold(log)
    if state["phase"] == "execution":
        log.append("phase.changed", {"from": "execution", "to": "documentation",
                                     "reason": "project documentation pass completed"})
    sync_state(project)

    _summarize(project, result, changed, violations, report_path)

    if not args.no_pr:
        _offer_pull_request(project, worktree, shipped, changed)

    if not args.keep_worktree:
        try:
            worktrees.remove(project, DOCS_NAME)
        except GitError:
            info("  docs  worktree kept (uncommitted changes)")

    return 1 if violations else 0


def _prompt(project: Project, shipped: list[TaskFile], base: str,
            unmerged: list[str], worktree) -> str:
    parts = [
        "You are running the phase 4 project documentation pass.",
        "",
        f"You are in a git worktree at the repository root, on branch "
        f"{worktree.branch}, branched from {base}. Paths in tool calls are "
        "relative to that root.",
        "",
        "Below is every ticket that has shipped, with its plan, its "
        "implementation report, and whatever its per-ticket documentation pass "
        "recorded. Tickets that have not shipped are deliberately absent; do not "
        "document them, and do not ask for them.",
        "",
        _evidence(project, shipped),
    ]

    if unmerged:
        parts += [
            "<warning>",
            "These shipped tickets are NOT merged into the ref you are "
            f"documenting ({base}): {', '.join(unmerged)}.",
            "Their code is not in this worktree. Do not describe their "
            "implementation as though you had read it; if you cannot verify "
            "something against the code in front of you, leave it out and say so "
            "in your report.",
            "</warning>",
            "",
        ]

    parts += [
        f"Required: {', '.join(REQUIRED_DOCS)} must all exist and describe the "
        "project as built when you are done.",
        "",
        "Carry out your contract, then return your result.",
    ]
    return "\n".join(parts)


def _summarize(project: Project, result: AgentResult, changed: list[str],
               violations: list[str], report_path) -> None:
    info("")
    info(f"Project type: {result.result.get('project_type') or 'not stated'}")
    info(f"Files changed: {len(changed)}")
    for path in changed:
        info(f"  {path}")

    inferred = result.result.get("inferred_docs", [])
    if inferred:
        info("")
        info("Inferred for this project type:")
        for entry in inferred:
            info(f"  {entry['path']} - {entry['justification']}")

    considered = result.result.get("files_considered", [])
    if considered:
        info("")
        info("Considered and left alone:")
        for entry in considered:
            info(f"  {entry['path']} - {entry['reason_left_alone']}")

    info("")
    info(f"Report: {rel(report_path, project.root)}")

    if violations:
        info("")
        info("Contract violations:")
        for violation in violations:
            info(f"  - {violation}")
        info("These are recorded in the event log. The documentation is on the "
             "branch; review it before opening a pull request.")


def _offer_pull_request(project: Project, worktree, shipped: list[TaskFile],
                        changed: list[str]) -> None:
    state = fold(open_log(project))
    repo_entry = state.get("repo")
    if not repo_entry:
        info("")
        info("No tracker repository is linked; the branch is committed but unpushed.")
        return

    repo = parse_repo_arg(repo_entry["full"])
    if repo is None:
        warn(f"cannot parse the linked repository {repo_entry['full']!r}")
        return

    body = "\n".join([
        "Project documentation pass (phase 4).",
        "",
        f"Documents {len(shipped)} shipped ticket(s): "
        + ", ".join(t.id for t in shipped),
        "",
        "Files changed:",
        *(f"- `{path}`" for path in changed),
        "",
        "Generated by `harness write-documentation` from the implementation "
        "reports and the code on the base ref. Only tickets a human approved "
        "were documented.",
    ])

    info("")
    info(f"Ready to push {worktree.branch} and open a DRAFT pull request on "
         f"{repo_entry['full']}.")
    if not confirm("Push the branch and open the draft PR?"):
        info("Skipped. The branch is committed locally and unpushed.")
        return

    try:
        tracker, _ = trackers.get(repo_entry.get("provider") or "github", project)
        worktrees.push(worktree)
        base = tracker.default_branch(repo)
        number, url = tracker.open_pull_request(
            repo, head=worktree.branch, base=base,
            title="Documentation: what shipped", body=body, draft=True)
    except (TrackerError, GitError) as exc:
        raise CommandError(f"could not open the pull request: {exc}") from exc

    open_log(project).append("docs.pr_opened", {
        "number": number, "url": url, "branch": worktree.branch,
        "base": base, "draft": True,
    }, stream=DOCS_NAME)
    sync_state(project)
    info(f"Draft PR #{number}  {url}")
