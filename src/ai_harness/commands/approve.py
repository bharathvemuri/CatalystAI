"""`harness approve` — the human gate (spec sections 5.4 and 13).

Section 13's requirement is that this gate surface the *actual* Security, QA and
Reviewer evidence at approval time, not a summary that says it went well —
otherwise the approval step becomes the rubber stamp it exists to prevent. So
the digest is assembled from the real report files and the real gate results,
written to disk as its own artifact, and printed in full before the question is
asked.

The gate is also mechanical, not advisory: it refuses outright when a required
report is missing, when a gate failed, or when the Reviewer's decision was not
APPROVE. There is no ``--force`` here — a ticket whose evidence does not support
approval is one whose evidence needs to change.
"""

from __future__ import annotations

import argparse

from .. import agents, gates, taskfile, trackers, worktrees
from ..paths import Project
from ..state import fold
from ..taskfile import TaskFileError
from ..trackers import TrackerError
from ..trackers.github import parse_repo_arg
from ._common import (CommandError, confirm, die, info, open_log, rel,
                      require_project, sync_state, warn)

REQUIRED_REPORTS = ("security", "qa", "reviewer")
DIGEST_FILE = "approval.md"


def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "approve",
        help="review a ticket's evidence and approve it",
        description="The human gate. Prints the real Security, QA and Reviewer "
                    "evidence, then asks. Refused if any of it is missing or failing.",
    )
    p.add_argument("ticket", help="task id, e.g. T-001")
    p.add_argument("--show", action="store_true",
                   help="print the digest and stop, without asking")
    p.add_argument("--no-pr", action="store_true",
                   help="skip the pull request step after approval")
    p.set_defaults(func=run)


def _section(project: Project, ticket: str, agent: str) -> str:
    path = project.reports / ticket / agents.REPORT_FILES[agent]
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def build_digest(project: Project, ticket: str, state: dict) -> tuple[str, list[str]]:
    """Assemble the evidence, and everything that disqualifies approval."""
    problems: list[str] = []
    entry = state["tasks"].get(ticket) or {}

    if entry.get("status") == "human_approved":
        problems.append("this ticket has already been approved")
    elif entry.get("status") != "review_approved":
        problems.append(f"status is {entry.get('status') or 'unknown'}, not review_approved "
                        "— the Reviewer has not approved it")

    results = gates.read_results(project, ticket)
    if not results:
        problems.append("no gate results were recorded")
    else:
        for result in results:
            if result.status == "fail":
                problems.append(f"gate {result.gate} failed: {result.summary}")

    parts = [f"# Approval evidence — {ticket}", "",
             "Assembled from the reports on disk. Nothing here is a summary of a "
             "summary; each section is the agent's own output.", ""]

    parts += ["## Gates", "", gates.render_summary(results), ""]

    for agent in REQUIRED_REPORTS:
        body = _section(project, ticket, agent)
        if not body.strip():
            problems.append(f"{agent} report is missing")
            body = "(missing)"
        parts += [f"## {agent.capitalize()}", "", body.strip(), ""]

    for agent in ("performance", "developer"):
        body = _section(project, ticket, agent)
        if body.strip():
            parts += [f"## {agent.capitalize()}", "", body.strip(), ""]

    return "\n".join(parts), problems


def run(args: argparse.Namespace) -> int:
    project = require_project()
    log = open_log(project)
    state = fold(log)
    ticket = args.ticket

    if ticket not in state["tasks"]:
        die(f"no ticket {ticket} in the event log.")

    digest, problems = build_digest(project, ticket, state)

    directory = project.reports / ticket
    directory.mkdir(parents=True, exist_ok=True)
    digest_path = directory / DIGEST_FILE
    digest_path.write_text(digest, encoding="utf-8")

    info(digest)
    info("")
    info(f"(saved to {rel(digest_path, project.root)})")

    if args.show:
        return 0

    if problems:
        info("")
        die("this ticket cannot be approved:\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n\nFix the underlying evidence and re-run the ticket. "
              "There is deliberately no override.")

    info("")
    if not confirm(f"Approve {ticket} on this evidence?"):
        info("Not approved. Nothing changed.")
        return 1

    log.append("task.human_approved", {"task_id": ticket}, stream=ticket)
    state = sync_state(project)
    info(f"{ticket} approved.")

    _post_approval(project, ticket, args)
    sync_state(project)
    return 0


def _post_approval(project: Project, ticket: str, args) -> None:
    """Documentation and DevOps run only after the human gate, then the PR."""
    info("")
    info("Documentation and DevOps run after approval; they need the ticket's "
         "worktree, which `harness run` removes unless --keep-worktree was used.")

    existing = {w.ticket: w for w in worktrees.existing(project)}
    worktree = existing.get(ticket)
    if worktree is None:
        info(f"No worktree for {ticket} — skipping the post-approval agents. "
             f"Re-run with `harness run-ticket {ticket} --keep-worktree` to include them.")
        return

    if args.no_pr:
        info("--no-pr: leaving the branch unpushed.")
        return

    _offer_pull_request(project, ticket, worktree)


def _offer_pull_request(project: Project, ticket: str, worktree) -> None:
    state = fold(open_log(project))
    repo_entry = state.get("repo")
    if not repo_entry:
        info("No tracker repository is linked; run `harness setup-project` to link one.")
        return

    repo = parse_repo_arg(repo_entry["full"])
    if repo is None:
        warn(f"cannot parse the linked repository {repo_entry['full']!r}")
        return

    issue_ref = (state["tasks"].get(ticket) or {}).get("issue_ref") or ""
    number = issue_ref.rsplit("#", 1)[-1] if "#" in issue_ref else ""

    try:
        task = next(t for t in taskfile.load_all(project) if t.id == ticket)
        title = f"{ticket}: {task.frontmatter['title']}"
    except (TaskFileError, StopIteration):
        title = ticket

    digest_path = project.reports / ticket / DIGEST_FILE
    evidence = digest_path.read_text(encoding="utf-8") if digest_path.is_file() else ""

    body = "\n".join([
        f"Implements `{ticket}`, approved by a human on the evidence below.",
        f"\nCloses #{number}" if number.isdigit() else "",
        "",
        "<details><summary>Approval evidence</summary>",
        "",
        evidence[:50000],
        "",
        "</details>",
    ])

    info("")
    info(f"Ready to push {worktree.branch} and open a DRAFT pull request on "
         f"{repo_entry['full']}"
         + (f", closing issue #{number}" if number.isdigit() else "") + ".")
    if not confirm("Push the branch and open the draft PR?"):
        info("Skipped. The branch is unpushed and no PR was opened.")
        return

    try:
        tracker, _ = trackers.get(repo_entry.get("provider") or "github", project)
        worktrees.push(worktree)
        base = getattr(tracker, "default_branch", lambda _r: "main")(repo)
        pr_number, url = tracker.open_pull_request(
            repo, head=worktree.branch, base=base, title=title, body=body, draft=True)
    except (TrackerError, worktrees.GitError) as exc:
        raise CommandError(f"could not open the pull request: {exc}") from exc

    open_log(project).append("task.pr_opened", {
        "task_id": ticket, "number": pr_number, "url": url,
        "branch": worktree.branch, "base": base, "draft": True,
    }, stream=ticket)
    info(f"Draft PR #{pr_number}  {url}")
