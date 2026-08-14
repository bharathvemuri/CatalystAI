"""Tests for phase 4 — documentation. No model calls and no network.

The pass itself needs a model. Everything that decides what it may document,
and everything that checks what it claims afterwards, is deterministic and is
what these cover.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from ai_harness import agents, cli, contracts, runner, worktrees
from ai_harness.agents import AgentResult
from ai_harness.commands import write_documentation as wd
from ai_harness.commands._common import CommandError
from ai_harness.events import EventLog
from ai_harness.paths import Project
from ai_harness.state import fold

from test_phase2 import write_task
from test_phase3 import CONTRACT_SECTIONS, RecordingExecutor


@pytest.fixture
def docs_project(tmp_path) -> Project:
    """A git project past phase 3: two tickets shipped, one still pending."""
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "Test"]):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    (root / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "init"],
                   capture_output=True, check=True)

    project = Project(root)
    project.ensure_layout()

    write_task(project, "T-001", "backend", phase=1, title="Auth API")
    write_task(project, "T-002", "frontend", phase=1, title="Login form")
    write_task(project, "T-003", "frontend", phase=2, title="Password reset")

    log = EventLog(project.events)
    log.append("harness.initialized", {})
    log.append("spec.finalized", {"forced": False})
    log.append("tasks.generated", {"tasks": [
        {"id": "T-001", "dir": "backend", "phase": 1, "title": "Auth API", "depends_on": []},
        {"id": "T-002", "dir": "frontend", "phase": 1, "title": "Login form", "depends_on": []},
        {"id": "T-003", "dir": "frontend", "phase": 2, "title": "Password reset",
         "depends_on": []},
    ]})
    log.append("phase.changed", {"from": "setup", "to": "execution"})
    for ticket in ("T-001", "T-002"):
        directory = project.reports / ticket
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "implementation-report.md").write_text(
            f"# {ticket}\n\nImplemented and tested.\n", encoding="utf-8")
        (directory / "implementation-plan.md").write_text(
            f"# Plan for {ticket}\n", encoding="utf-8")
        log.append("task.human_approved", {"task_id": ticket}, stream=ticket)
    return project


def fake_result(**overrides) -> AgentResult:
    result = {
        "status": "complete",
        "blocking_reason": "",
        "summary": "Documented what shipped.",
        "report_markdown": "# Documentation report\n\nAll three files written.",
        "project_type": "python service",
        "files_touched": [
            {"path": "README.md", "change": "written", "driven_by": "T-001"},
            {"path": "ARCHITECTURE.md", "change": "written", "driven_by": "T-001"},
            {"path": "CONTRIBUTING.md", "change": "written", "driven_by": "T-002"},
        ],
        "files_considered": [{"path": "docs/legacy.md", "reason_left_alone": "untouched by any shipped ticket"}],
        "inferred_docs": [{"path": "API.md", "justification": "backend/ directory implies an API"}],
        "examples_run": [{"command": "python -m pytest", "exit_code": 0}],
    }
    result.update(overrides)
    return AgentResult(agent="documentation", model="claude-sonnet-5", effort="medium",
                       result=result, transcript=[], violations=[])


def install_fakes(monkeypatch, agent_result=None, *, writes=wd.REQUIRED_DOCS):
    """Replace the model call and the container with deterministic stand-ins."""
    # The credential check now resolves a backend rather than asking one
    # provider whether it has a key, so the seam to pin is the selection.
    monkeypatch.setattr(runner, "select", lambda preference=None: runner.API)

    def fake_executor(project, name, root, args, log):
        return RecordingExecutor(root), "/gates", "/reports", "sh"

    monkeypatch.setattr(wd, "make_executor", fake_executor)

    def fake_run(agent, *, root, **kwargs):
        for name in writes:
            (root / name).write_text(f"# {name}\n\nGenerated.\n", encoding="utf-8")
        return agent_result or fake_result()

    monkeypatch.setattr(agents, "run", fake_run)


def invoke(monkeypatch, project, *argv):
    monkeypatch.chdir(project.root)
    args = cli.build_parser().parse_args(["write-documentation", *argv])
    return wd.run(args)


# ------------------------------------------------------------------ contract

def test_project_contract_follows_the_standard_agent_contract(docs_project):
    contract = agents.load_contract(docs_project, "documentation-project")
    missing = [s for s in CONTRACT_SECTIONS if s not in contract]
    assert not missing, f"missing sections: {missing}"
    assert "Agents decide." in contract  # the shared preamble came along


def test_the_project_pass_is_a_role_of_the_documentation_agent(docs_project):
    assert "documentation-project" in agents.ROLES
    assert "documentation-project" not in agents.AGENTS
    assert agents.ROLE_AGENT["documentation-project"] == "documentation"
    assert "documentation-project" in agents.RESULT_SCHEMAS


def test_the_project_pass_still_cannot_write_source(docs_project):
    """The role changes the mission, never the boundary."""
    assert agents.may_write("documentation", "README.md")
    assert agents.may_write("documentation", "docs/api.md")
    assert not agents.may_write("documentation", "src/app.py")
    assert not agents.may_write("documentation", "Dockerfile")


# -------------------------------------------------------------- what shipped

def test_only_approved_and_done_tickets_are_documented(docs_project):
    from ai_harness import taskfile
    state = fold(EventLog(docs_project.events))
    shipped = wd.shipped_tickets(state, taskfile.load_all(docs_project))
    assert [t.id for t in shipped] == ["T-001", "T-002"]


def test_evidence_withholds_tickets_that_have_not_shipped(docs_project):
    from ai_harness import taskfile
    state = fold(EventLog(docs_project.events))
    shipped = wd.shipped_tickets(state, taskfile.load_all(docs_project))
    evidence = wd._evidence(docs_project, shipped)

    assert "T-001" in evidence and "T-002" in evidence
    assert "T-003" not in evidence
    assert "Implemented and tested." in evidence


def test_nothing_shipped_is_a_clean_refusal(docs_project, monkeypatch):
    log = EventLog(docs_project.events)
    log.append("tasks.generated", {"tasks": [
        {"id": "T-004", "dir": "backend", "phase": 1, "title": "New", "depends_on": []}]})
    # Reset every ticket to pending by starting from a fresh project instead.
    fresh = Project(docs_project.root)
    for path in (fresh.events).glob("*.jsonl"):
        path.unlink()
    log = EventLog(fresh.events)
    log.append("harness.initialized", {})
    log.append("spec.finalized", {"forced": False})
    log.append("tasks.generated", {"tasks": [
        {"id": "T-001", "dir": "backend", "phase": 1, "title": "Auth API", "depends_on": []}]})

    install_fakes(monkeypatch)
    with pytest.raises(CommandError, match="nothing has shipped"):
        invoke(monkeypatch, fresh, "--yes", "--no-pr")


# ------------------------------------------------------------- merge checking

def test_unmerged_ticket_branches_are_detected(docs_project):
    worktree = worktrees.ensure(docs_project, "T-001")
    (worktree.path / "feature.py").write_text("x = 1\n", encoding="utf-8")
    worktrees.commit_all(worktree, "T-001: work")

    assert not worktrees.is_merged(docs_project.root, "harness/T-001", "main")

    subprocess.run(["git", "-C", str(docs_project.root), "merge", "--no-ff", "-m", "merge",
                    "harness/T-001"], capture_output=True, check=True)
    assert worktrees.is_merged(docs_project.root, "harness/T-001", "main")


def test_a_branch_that_does_not_exist_is_not_merged(docs_project):
    assert not worktrees.is_merged(docs_project.root, "harness/T-999", "main")


def test_a_deleted_ticket_branch_is_not_reported_as_unmerged(docs_project):
    """The usual reason a ticket branch is gone is that its PR merged. A warning
    that fires on the normal path is a warning nobody reads."""
    from ai_harness import taskfile
    state = fold(EventLog(docs_project.events))
    shipped = wd.shipped_tickets(state, taskfile.load_all(docs_project))
    assert wd._unmerged(docs_project, shipped, "main") == []


def test_the_plan_warns_about_unmerged_tickets(docs_project, monkeypatch, capsys):
    worktree = worktrees.ensure(docs_project, "T-001")
    (worktree.path / "feature.py").write_text("x = 1\n", encoding="utf-8")
    worktrees.commit_all(worktree, "T-001: work")

    install_fakes(monkeypatch)
    assert invoke(monkeypatch, docs_project, "--dry-run") == 0

    out = capsys.readouterr().out
    assert "NOT MERGED INTO BASE" in out
    assert "T-003" in out and "have not shipped" in out


# ------------------------------------------------------------- verification

def test_missing_required_document_is_a_violation(docs_project, monkeypatch, capsys):
    install_fakes(monkeypatch, writes=("README.md", "ARCHITECTURE.md"))
    assert invoke(monkeypatch, docs_project, "--yes", "--no-pr") == 1

    out = capsys.readouterr().out
    assert "required document(s) absent" in out and "CONTRIBUTING.md" in out

    completed = [e for e in EventLog(docs_project.events).read_all()
                 if e.type == "docs.pass_completed"]
    assert completed[0].payload["required_present"] is False


def test_undeclared_changes_are_caught(docs_project, monkeypatch, capsys):
    """git is the witness, not the agent's own file list."""
    result = fake_result(files_touched=[
        {"path": "README.md", "change": "written", "driven_by": "T-001"}])
    install_fakes(monkeypatch, result)
    assert invoke(monkeypatch, docs_project, "--yes", "--no-pr") == 1

    out = capsys.readouterr().out
    assert "changed but not declared" in out
    assert "ARCHITECTURE.md" in out and "CONTRIBUTING.md" in out


def test_claiming_a_file_that_was_not_written_is_caught(docs_project, monkeypatch, capsys):
    result = fake_result(files_touched=[
        *fake_result().result["files_touched"],
        {"path": "GOVERNANCE.md", "change": "invented", "driven_by": "T-001"}])
    install_fakes(monkeypatch, result)
    assert invoke(monkeypatch, docs_project, "--yes", "--no-pr") == 1
    assert "declared as touched but unchanged" in capsys.readouterr().out


def test_a_clean_pass_reports_no_violations(docs_project, monkeypatch, capsys):
    install_fakes(monkeypatch)
    assert invoke(monkeypatch, docs_project, "--yes", "--no-pr") == 0
    assert "Contract violations" not in capsys.readouterr().out


# ---------------------------------------------------------------- the pass

def test_a_full_pass_writes_commits_and_records(docs_project, monkeypatch):
    install_fakes(monkeypatch)
    assert invoke(monkeypatch, docs_project, "--yes", "--no-pr", "--keep-worktree") == 0

    worktree = next(w for w in worktrees.existing(docs_project) if w.ticket == "docs")
    assert worktree.branch == "harness/docs"
    for name in wd.REQUIRED_DOCS:
        assert (worktree.path / name).is_file()
    assert not worktrees.is_dirty(worktree)  # committed

    report = docs_project.reports / "documentation" / "documentation-report.md"
    assert "Documentation report" in report.read_text(encoding="utf-8")


def test_the_pass_advances_the_phase_and_records_state(docs_project, monkeypatch):
    install_fakes(monkeypatch)
    invoke(monkeypatch, docs_project, "--yes", "--no-pr")

    state = fold(EventLog(docs_project.events))
    assert state["phase"] == "documentation"

    docs = state["documentation"]
    assert docs["tickets_documented"] == ["T-001", "T-002"]
    assert docs["required_present"] is True
    assert sorted(docs["files_touched"]) == sorted(wd.REQUIRED_DOCS)
    assert docs["pr"] is None

    contracts.validate("state", {k: v for k, v in state.items()
                                 if not k.startswith("_")}, docs_project)
    for path in sorted(docs_project.events.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            contracts.validate("event", json.loads(line), docs_project)


def test_the_worktree_is_removed_unless_kept(docs_project, monkeypatch):
    install_fakes(monkeypatch)
    invoke(monkeypatch, docs_project, "--yes", "--no-pr")
    assert [w for w in worktrees.existing(docs_project) if w.ticket == "docs"] == []


def test_dry_run_creates_no_worktree_and_no_events(docs_project, monkeypatch):
    install_fakes(monkeypatch)
    before = len(EventLog(docs_project.events).read_all())

    assert invoke(monkeypatch, docs_project, "--dry-run") == 0
    assert worktrees.existing(docs_project) == []
    assert len(EventLog(docs_project.events).read_all()) == before
    assert not (docs_project.root / "ARCHITECTURE.md").exists()


def test_declining_the_prompt_does_nothing(docs_project, monkeypatch):
    install_fakes(monkeypatch)
    monkeypatch.setattr(wd, "confirm", lambda prompt: False)
    assert invoke(monkeypatch, docs_project) == 1
    assert worktrees.existing(docs_project) == []


def test_a_blocked_agent_stops_without_advancing_the_phase(docs_project, monkeypatch, capsys):
    blocked = fake_result(status="blocked",
                          blocking_reason="an implementation report is missing")
    install_fakes(monkeypatch, blocked)
    assert invoke(monkeypatch, docs_project, "--yes", "--no-pr") == 1

    assert "an implementation report is missing" in capsys.readouterr().out
    state = fold(EventLog(docs_project.events))
    assert state["phase"] == "execution"
    assert state["documentation"] is None


def test_isolation_waiver_is_recorded(docs_project, monkeypatch):
    """--no-container is a real reduction in evidence quality, so it is logged."""
    # The credential check now resolves a backend rather than asking one
    # provider whether it has a key, so the seam to pin is the selection.
    monkeypatch.setattr(runner, "select", lambda preference=None: runner.API)
    monkeypatch.setattr(agents, "run", lambda agent, *, root, **kw: (
        [(root / n).write_text("# x\n", encoding="utf-8") for n in wd.REQUIRED_DOCS],
        fake_result())[1])

    real_executor = wd.make_executor

    def spy(project, name, root, args, log):
        assert args.no_container
        return real_executor(project, name, root, args, log)

    monkeypatch.setattr(wd, "make_executor", spy)
    invoke(monkeypatch, docs_project, "--yes", "--no-pr", "--no-container")

    waived = [e for e in EventLog(docs_project.events).read_all()
              if e.type == "ticket.isolation_waived"]
    assert waived and waived[0].payload["task_id"] == "docs"
