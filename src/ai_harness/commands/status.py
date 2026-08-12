"""`harness status` and `harness log` — read-only views over the event log."""

from __future__ import annotations

import argparse
import json

from ..contracts import ContractViolation, validate
from ..state import fold
from ._common import info, open_log, require_project, rel, sync_state, warn


def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "status", help="show the current derived state",
        description="Re-renders state.json from the event log and prints a summary.",
    )
    p.add_argument("--json", action="store_true", help="print the raw derived state")
    p.set_defaults(func=run_status)

    q = subparsers.add_parser(
        "log", help="print the event log",
        description="The append-only source of truth, oldest first.",
    )
    q.add_argument("-n", "--tail", type=int, metavar="N", help="show only the last N events")
    q.add_argument("--stream", metavar="NAME", help="restrict to one stream")
    q.add_argument("--json", action="store_true", help="print raw JSON lines")
    q.set_defaults(func=run_log)


def run_status(args: argparse.Namespace) -> int:
    project = require_project()
    state = sync_state(project)

    try:
        validate("state", {k: v for k, v in state.items() if not k.startswith("_")}, project)
    except ContractViolation as exc:
        warn(f"derived state does not satisfy its own contract:\n{exc}")

    if args.json:
        print(json.dumps(state, indent=2))
        return 0

    spec = state["spec"]
    info(f"project   {project.root}")
    info(f"phase     {state['phase']}")
    info(f"spec      {spec['status']}  "
         f"(revision {spec['revision']}, {spec['open_questions']} open question(s))")

    repo = state.get("repo")
    if repo:
        info(f"repo      {repo['provider']}:{repo['full']}"
             + ("  (created by the harness)" if repo.get("created") else ""))

    tasks = state["tasks"]
    if not tasks:
        info("tasks     none yet")
    else:
        counts: dict[str, int] = {}
        for task in tasks.values():
            counts[task["status"]] = counts.get(task["status"], 0) + 1
        summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
        info(f"tasks     {len(tasks)} total — {summary}")

        linked = sum(1 for t in tasks.values() if t.get("issue_ref"))
        if linked:
            info(f"issues    {linked} of {len(tasks)} task(s) linked to a tracker issue")

        ready = [tid for tid, t in tasks.items()
                 if t["status"] == "pending"
                 and all(tasks.get(d, {}).get("status") == "done" for d in t["depends_on"])]
        if ready:
            info(f"ready     {', '.join(sorted(ready))}  (dependencies satisfied)")

    if state["human_approvals_pending"]:
        info(f"awaiting  human approval: {', '.join(state['human_approvals_pending'])}")

    info("")
    info(f"state view  {rel(project.state_file, project.root)}  (derived — do not edit)")
    info(f"event log   {rel(project.events, project.root)}/")
    return 0


def run_log(args: argparse.Namespace) -> int:
    project = require_project()
    log = open_log(project)
    events = log.read_stream(args.stream) if args.stream else log.read_all()

    if args.tail:
        events = events[-args.tail:]

    if not events:
        info("no events")
        return 0

    for event in events:
        if args.json:
            print(event.to_json())
            continue
        summary = _summarize(event)
        info(f"{event.ts}  {event.stream:<10} {event.type:<32} {summary}")
    return 0


def _summarize(event) -> str:
    p = event.payload
    if event.type == "spec.round_opened":
        return f"round {p.get('round')}, {len(p.get('questions', []))} question(s)"
    if event.type == "spec.answers_recorded":
        deferred = len(p.get("deferred", []))
        return (f"round {p.get('round')}, {len(p.get('answers', []))} answered"
                + (f", {deferred} deferred" if deferred else ""))
    if event.type == "spec.finalized":
        return "forced closure" if p.get("forced") else "approved"
    if event.type == "tasks.generated":
        return f"{len(p.get('tasks', []))} task(s)"
    if event.type == "task.status_changed":
        return f"{p.get('task_id')} → {p.get('status')}"
    if event.type == "tracker.repo_linked":
        return f"{p.get('provider')}:{p.get('repo')}" + (" (created)" if p.get("created") else "")
    if event.type == "tracker.metadata_synced":
        return (f"{len(p.get('labels_created', []))} label(s), "
                f"{len(p.get('milestones_created', []))} milestone(s) created")
    if event.type == "task.issue_linked":
        return f"{p.get('task_id')} → {p.get('issue_ref')}"
    if event.type == "task.issue_reconciled":
        return f"{p.get('task_id')} {p.get('issue_ref')}: {', '.join(p.get('changes', []))}"
    return ""
