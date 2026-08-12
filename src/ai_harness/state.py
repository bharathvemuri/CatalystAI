"""Derived state — a left fold over the event log.

Nothing here is authoritative. ``state.json`` is rendered from the log for human
and tool consumption; deleting it loses nothing. Every field in spec section 2
is reproduced, so anything reading ``state.json`` sees the shape the spec
promised even though the storage underneath changed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import HARNESS_VERSION
from .events import Event, EventLog

PHASES = ("spec_review", "chunking", "setup", "execution", "documentation", "production")
SPEC_STATUSES = ("draft", "in_review", "approved")
TASK_STATUSES = ("pending", "in_progress", "blocked", "review_approved", "human_approved", "done")


def empty_state() -> dict[str, Any]:
    return {
        "harness_version": HARNESS_VERSION,
        "phase": "spec_review",
        "spec": {"status": "draft", "open_questions": 0, "revision": 0},
        "repo": None,
        "tasks": {},
        "human_approvals_pending": [],
    }


def _task(state: dict[str, Any], task_id: str) -> dict[str, Any]:
    return state["tasks"].setdefault(task_id, {
        "dir": None,
        "status": "pending",
        "depends_on": [],
        "issue_ref": None,
        "current_agent": None,
        "assigned_models": {},
        "history": [],
    })


def apply(state: dict[str, Any], event: Event) -> dict[str, Any]:
    """Apply one event. Unknown event types are ignored, not fatal — a newer
    harness writing events an older one doesn't understand must not break it."""
    p = event.payload
    t = event.type

    if t == "harness.initialized":
        state["phase"] = "spec_review"

    elif t == "phase.changed":
        if p.get("to") in PHASES:
            state["phase"] = p["to"]

    elif t == "spec.round_opened":
        state["spec"]["status"] = "in_review"
        state["spec"]["open_questions"] = len(p.get("questions", []))

    elif t == "spec.answers_recorded":
        state["spec"]["revision"] = state["spec"]["revision"] + 1
        answered = sum(1 for a in p.get("answers", []) if a.get("answer"))
        state["spec"]["open_questions"] = max(
            0, state["spec"]["open_questions"] - answered
        )

    elif t == "spec.review_clean":
        state["spec"]["open_questions"] = 0

    elif t == "spec.finalized":
        state["spec"]["status"] = "approved"
        state["spec"]["open_questions"] = 0 if not p.get("forced") else state["spec"]["open_questions"]
        state["phase"] = "chunking"

    elif t == "tasks.generated":
        for task in p.get("tasks", []):
            entry = _task(state, task["id"])
            entry["dir"] = task.get("dir")
            entry["depends_on"] = task.get("depends_on", [])
            entry["status"] = "pending"
        state["phase"] = "setup"

    elif t == "tracker.repo_linked":
        state["repo"] = {
            "provider": p.get("provider"),
            "full": p.get("repo"),
            "url": p.get("url"),
            "created": bool(p.get("created")),
        }

    elif t == "task.issue_reconciled":
        _task(state, p["task_id"])["issue_ref"] = p.get("issue_ref")

    elif t == "task.status_changed":
        entry = _task(state, p["task_id"])
        if p.get("status") in TASK_STATUSES:
            entry["status"] = p["status"]

    elif t == "task.models_assigned":
        _task(state, p["task_id"])["assigned_models"] = p.get("assigned_models", {})

    elif t == "task.agent_started":
        _task(state, p["task_id"])["current_agent"] = p.get("agent")

    elif t == "task.agent_completed":
        entry = _task(state, p["task_id"])
        entry["history"].append({"agent": p.get("agent"), "result": p.get("result"), "ts": event.ts})
        entry["current_agent"] = None

    elif t == "task.review_decided":
        entry = _task(state, p["task_id"])
        entry["history"].append({"agent": "reviewer", "result": p.get("decision"),
                                 "ts": event.ts})
        if p.get("decision") == "APPROVE":
            entry["status"] = "review_approved"

    elif t == "task.issue_linked":
        _task(state, p["task_id"])["issue_ref"] = p.get("issue_ref")

    elif t == "task.human_approval_requested":
        if p["task_id"] not in state["human_approvals_pending"]:
            state["human_approvals_pending"].append(p["task_id"])

    elif t == "task.human_approved":
        if p["task_id"] in state["human_approvals_pending"]:
            state["human_approvals_pending"].remove(p["task_id"])
        _task(state, p["task_id"])["status"] = "human_approved"

    return state


def fold(log: EventLog) -> dict[str, Any]:
    state = empty_state()
    for event in log.read_all():
        state = apply(state, event)
    return state


def render(log: EventLog, out_file: Path) -> dict[str, Any]:
    """Fold the log and write the derived view atomically."""
    state = fold(log)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_file.with_suffix(out_file.suffix + ".tmp")
    banner = {
        "_generated": "DERIVED VIEW — do not edit. Rendered from .harness/events/*.jsonl",
        **state,
    }
    tmp.write_text(json.dumps(banner, indent=2) + "\n", encoding="utf-8")
    tmp.replace(out_file)
    return state
