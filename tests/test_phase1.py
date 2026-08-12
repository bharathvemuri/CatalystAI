"""Tests for the deterministic parts of phase 1 — no API calls."""

from __future__ import annotations

import json

import pytest

from ai_harness import contracts, qa_round
from ai_harness.commands import chunk_specs
from ai_harness.commands._common import CommandError
from ai_harness.events import EventLog
from ai_harness.paths import Project
from ai_harness.registry import Registry
from ai_harness.state import fold, render


@pytest.fixture
def project(tmp_path) -> Project:
    p = Project(tmp_path)
    p.ensure_layout()
    return p


# ------------------------------------------------------------------ event log

def test_log_is_append_only_and_ordered(project):
    log = EventLog(project.events)
    log.append("harness.initialized", {})
    log.append("spec.round_opened", {"round": 1, "questions": [{"id": "q1"}]})
    log.append("spec.answers_recorded", {"round": 1, "answers": [{"id": "q1", "answer": "yes"}]})

    events = log.read_all()
    assert [e.type for e in events] == [
        "harness.initialized", "spec.round_opened", "spec.answers_recorded"]

    raw = (project.events / "main.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(raw) == 3
    for line in raw:
        contracts.validate("event", json.loads(line), project)


def test_streams_do_not_interleave(project):
    """Per-stream segments are what make parallel worktrees safe."""
    log = EventLog(project.events)
    log.append("task.status_changed", {"task_id": "T-001", "status": "in_progress"}, stream="T-001")
    log.append("task.status_changed", {"task_id": "T-002", "status": "in_progress"}, stream="T-002")
    log.append("task.status_changed", {"task_id": "T-001", "status": "done"}, stream="T-001")

    assert sorted(log.streams()) == ["T-001", "T-002"]
    assert [e.payload["status"] for e in log.read_stream("T-001")] == ["in_progress", "done"]
    assert len(log.read_all()) == 3


def test_torn_final_line_does_not_lose_earlier_events(project):
    log = EventLog(project.events)
    log.append("harness.initialized", {})
    with (project.events / "main.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"v": "0.1", "id": "ev_dead')  # writer died mid-append

    events = log.read_all()
    assert len(events) == 1
    assert events[0].type == "harness.initialized"


def test_unsafe_stream_name_rejected(project):
    with pytest.raises(ValueError):
        EventLog(project.events).append("harness.initialized", {}, stream="../escape")


# ------------------------------------------------------------- derived state

def test_state_is_a_pure_fold(project):
    log = EventLog(project.events)
    log.append("harness.initialized", {})
    log.append("spec.round_opened", {"round": 1, "questions": [{"id": "a"}, {"id": "b"}]})
    log.append("spec.answers_recorded", {"round": 1, "answers": [{"id": "a", "answer": "x"}]})
    log.append("spec.review_clean", {"round": 2})
    log.append("spec.finalized", {"forced": False})

    state = fold(log)
    assert state["spec"]["status"] == "approved"
    assert state["spec"]["open_questions"] == 0
    assert state["spec"]["revision"] == 1
    assert state["phase"] == "chunking"
    contracts.validate("state", state, project)


def test_state_json_is_regenerated_not_authoritative(project):
    log = EventLog(project.events)
    log.append("harness.initialized", {})
    render(log, project.state_file)

    project.state_file.write_text('{"phase": "production"}', encoding="utf-8")
    state = render(log, project.state_file)

    assert state["phase"] == "spec_review"
    assert "DERIVED VIEW" in project.state_file.read_text(encoding="utf-8")


def test_unknown_event_types_are_ignored(project):
    log = EventLog(project.events)
    log.append("harness.initialized", {})
    with (project.events / "main.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"v": "9.9", "id": "ev_" + "0" * 16, "ts": "2099-01-01T00:00:00.000000Z",
                             "stream": "main", "type": "future.thing", "payload": {}}) + "\n")

    assert fold(log)["phase"] == "spec_review"


def test_task_lifecycle_and_approval_queue(project):
    log = EventLog(project.events)
    log.append("tasks.generated", {"tasks": [
        {"id": "T-001", "dir": "backend", "depends_on": []},
        {"id": "T-002", "dir": "frontend", "depends_on": ["T-001"]},
    ]})
    log.append("task.human_approval_requested", {"task_id": "T-001"})
    log.append("task.human_approved", {"task_id": "T-001"})

    state = fold(log)
    assert state["tasks"]["T-002"]["depends_on"] == ["T-001"]
    assert state["tasks"]["T-001"]["status"] == "human_approved"
    assert state["human_approvals_pending"] == []


# ----------------------------------------------------------- Q&A round file

def test_round_file_round_trips():
    questions = [
        qa_round.Question(id="q_scope", question="Does import roll back per batch or per row?",
                          why_it_matters="Determines the transaction boundary.",
                          source="section 4", severity="blocking",
                          options=["per batch", "per row"]),
        qa_round.Question(id="q_minor", question="What timezone for display?",
                          why_it_matters="Affects the formatter.",
                          source="section 7", severity="minor", options=[]),
    ]
    text = qa_round.render(questions, round_no=3, assessment="Thin on error paths.")
    assert qa_round.round_number(text) == 3

    filled = text.replace(
        qa_round.ANSWER_MARKER,
        qa_round.ANSWER_MARKER + "\n\nPer row, and report failures separately.", 1)
    answers = qa_round.parse(filled)

    by_id = {a.id: a for a in answers}
    assert by_id["q_scope"].answered
    assert "Per row" in by_id["q_scope"].answer
    assert not by_id["q_minor"].answered  # blank stays open; never assumed


def test_blocking_questions_are_ordered_first():
    questions = [
        qa_round.Question("q_a", "minor one", "why", "src", "minor"),
        qa_round.Question("q_b", "blocking one", "why", "src", "blocking"),
    ]
    text = qa_round.render(questions, round_no=1)
    assert text.index("qid=q_b") < text.index("qid=q_a")


def test_multiline_answers_survive_parsing():
    q = [qa_round.Question("q1", "Question?", "why", "src", "important")]
    text = qa_round.render(q, round_no=1)
    body = "Line one.\n\n- bullet\n- another\n\nClosing line."
    answers = qa_round.parse(text.replace(qa_round.ANSWER_MARKER,
                                          qa_round.ANSWER_MARKER + "\n\n" + body, 1))
    assert answers[0].answer == body


def test_revised_spec_is_append_only(project):
    a1 = [qa_round.Answer("q1", "First?", "yes")]
    a2 = [qa_round.Answer("q2", "Second?", "no")]
    qa_round.append_to_revised_spec(project.revised_spec, 1, "2026-01-01T00:00:00Z", a1)
    qa_round.append_to_revised_spec(project.revised_spec, 2, "2026-01-02T00:00:00Z", a2)

    text = project.revised_spec.read_text(encoding="utf-8")
    assert "Round 1" in text and "Round 2" in text
    assert text.index("Round 1") < text.index("Round 2")
    assert text.count("# Revised specification") == 1


# ------------------------------------------------------- dependency graph

def _task(tid, deps):
    return {"id": tid, "depends_on": deps}


def test_valid_graph_passes():
    chunk_specs._validate_graph([
        _task("T-001", []), _task("T-002", ["T-001"]), _task("T-003", ["T-001", "T-002"])])


def test_cycle_is_rejected():
    with pytest.raises(CommandError, match="cycle"):
        chunk_specs._validate_graph([_task("T-001", ["T-002"]), _task("T-002", ["T-001"])])


def test_self_dependency_is_rejected():
    with pytest.raises(CommandError, match="itself"):
        chunk_specs._validate_graph([_task("T-001", ["T-001"])])


def test_unknown_dependency_is_rejected():
    with pytest.raises(CommandError, match="unknown task"):
        chunk_specs._validate_graph([_task("T-001", ["T-999"])])


def test_duplicate_ids_are_rejected():
    with pytest.raises(CommandError, match="duplicate"):
        chunk_specs._validate_graph([_task("T-001", []), _task("T-001", [])])


# ------------------------------------------------------------- task contract

def _valid_task():
    return {
        "id": "T-001", "title": "Implement login form", "phase": 1, "dir": "frontend",
        "depends_on": [], "inputs": ["revised-spec.md#section-3.2"],
        "start_condition": "auth API contract is finalised",
        "done_condition": "Login form renders, submits, and passes QA",
        "acceptance_criteria": ["Form validates email format client-side"],
        "status": "pending",
    }


def test_task_contract_accepts_a_well_formed_task(project):
    contracts.validate("task", _valid_task(), project)


@pytest.mark.parametrize("mutate", [
    lambda t: t.update(id="task-1"),
    lambda t: t.update(dir="Frontend"),
    lambda t: t.update(acceptance_criteria=[]),
    lambda t: t.update(status="almost_done"),
    lambda t: t.pop("done_condition"),
    lambda t: t.update(surprise="extra field"),
])
def test_task_contract_rejects_malformed_tasks(project, mutate):
    task = _valid_task()
    mutate(task)
    with pytest.raises(contracts.ContractViolation):
        contracts.validate("task", task, project)


# ---------------------------------------------------------------- registry

def test_registry_rejects_models_outside_the_closed_set(project):
    registry = Registry.load(project)
    assert registry.validate("claude-opus-5") == "claude-opus-5"
    with pytest.raises(ValueError, match="not in the registry"):
        registry.validate("gpt-4")


def test_registry_never_cost_optimises_security_below_the_top_tier(project):
    registry = Registry.load(project)
    entry = {m.id: m for m in registry.models()}[registry.default_for("security")]
    assert entry.tier == "high-capability"


def test_project_override_wins_over_package_default(project):
    override = project.harness / "overrides" / "model-registry.yaml"
    override.write_text(
        "version: '0.1'\n"
        "providers:\n  anthropic:\n    models:\n"
        "      - {id: claude-haiku-4-5, tier: fast, cost: low}\n"
        "defaults:\n  architect: claude-haiku-4-5\n",
        encoding="utf-8")
    registry = Registry.load(project)
    assert registry.default_for("architect") == "claude-haiku-4-5"
    assert not registry.known("claude-opus-5")
