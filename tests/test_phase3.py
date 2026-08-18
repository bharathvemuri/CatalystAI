"""Tests for phase 3 — execution. No model calls and no network.

The agents themselves need a model; everything around them — contracts,
boundary enforcement, gates, worktrees, the review gate, the approval gate —
is deterministic and is what these cover.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from ai_harness import agents, gates, pipeline, worktrees
from ai_harness.agents import AgentError, AgentResult, ToolBox
from ai_harness.commands import approve
from ai_harness.commands._common import CommandError
from ai_harness.context_index import ContextIndex, available as index_available
from ai_harness.events import EventLog
from ai_harness.execution import CommandResult, Executor, HostExecutor, posix_shell
from ai_harness.gates import GateResult
from ai_harness.paths import Project
from ai_harness.registry import Registry
from ai_harness.state import fold
from ai_harness.thresholds import Thresholds

from test_phase2 import write_task

CONTRACT_SECTIONS = ["## Mission", "## Inputs", "## Responsibilities",
                     "## Non-Responsibilities", "## Required Tools",
                     "## Required Output", "## Blocking Conditions",
                     "## Success Criteria", "## Failure Conditions"]


@pytest.fixture
def project(tmp_path) -> Project:
    p = Project(tmp_path)
    p.ensure_layout()
    return p


class RecordingExecutor(Executor):
    """Stands in for a container. Records commands instead of running them."""

    def __init__(self, root, results=None):
        super().__init__(root=root)
        self.commands: list[list[str]] = []
        self.results = results or {}

    def run(self, argv, *, cwd=None, env=None, timeout=1800) -> CommandResult:
        self.commands.append(list(argv))
        stdout, code = self.results.get(argv[-1], ("", 0))
        return CommandResult(argv=list(argv), exit_code=code, stdout=stdout,
                             stderr="", duration_ms=1, where="recording")

    def describe(self) -> str:
        return "recording"


# ------------------------------------------------------------------ contracts

def test_every_registry_agent_has_a_contract(project):
    registry = Registry.load(project)
    for agent in agents.AGENTS:
        assert registry.default_for(agent)
        assert registry.effort_for(agent)
        contract = agents.load_contract(project, agent)
        assert contract.strip()


@pytest.mark.parametrize("agent", agents.AGENTS)
def test_contracts_follow_the_standard_agent_contract(project, agent):
    contract = agents.load_contract(project, agent)
    missing = [s for s in CONTRACT_SECTIONS if s not in contract]
    assert not missing, f"{agent} is missing {missing}"


def test_contract_includes_the_shared_preamble(project):
    contract = agents.load_contract(project, "security")
    assert "CLAIM" in contract and "EVIDENCE" in contract
    assert "Agents decide." in contract


def test_unknown_role_is_refused(project):
    with pytest.raises(AgentError, match="unknown role"):
        agents.load_contract(project, "wizard")


# ------------------------------------------------------------- boundaries

@pytest.mark.parametrize("agent,path,allowed", [
    ("developer", "src/app.py", True),
    ("developer", "tests/test_app.py", True),
    ("architect", "src/app.py", False),
    ("architect", "README.md", False),
    ("security", "src/app.py", False),
    ("reviewer", "src/app.py", False),
    ("qa", "tests/test_app.py", True),
    ("qa", "src/app.py", False),
    ("performance", "bench/bench_api.py", True),
    ("performance", "src/app.py", False),
    ("documentation", "README.md", True),
    ("documentation", "docs/guide.md", True),
    ("documentation", "src/app.py", False),
    ("devops", "Dockerfile", True),
    ("devops", ".github/workflows/ci.yml", True),
    ("devops", "src/app.py", False),
])
def test_write_boundaries_match_the_contract_table(agent, path, allowed):
    assert agents.may_write(agent, path) is allowed


def test_read_only_agents_are_not_given_write_tools():
    for agent in ("architect", "security", "reviewer"):
        names = {t["name"] for t in agents.tools_for(agent)}
        assert "write_file" not in names and "edit_file" not in names
    assert "write_file" in {t["name"] for t in agents.tools_for("developer")}


def test_a_write_outside_the_boundary_is_refused_and_recorded(project):
    (project.root / "src").mkdir()
    box = ToolBox("security", project.root, RecordingExecutor(project.root))

    output, is_error = box.dispatch("write_file", {"path": "src/app.py", "content": "x"})
    assert is_error and "refused" in output
    assert box.violations == ["security attempted to write src/app.py"]
    assert not (project.root / "src" / "app.py").exists()


def test_a_permitted_write_succeeds(project):
    box = ToolBox("developer", project.root, RecordingExecutor(project.root))
    output, is_error = box.dispatch("write_file", {"path": "src/app.py", "content": "print(1)\n"})
    assert not is_error and "wrote" in output
    assert (project.root / "src" / "app.py").read_text(encoding="utf-8") == "print(1)\n"
    assert box.violations == []


def test_paths_cannot_escape_the_worktree(project):
    box = ToolBox("developer", project.root, RecordingExecutor(project.root))
    output, is_error = box.dispatch("write_file", {"path": "../escape.py", "content": "x"})
    assert is_error and "outside the ticket worktree" in output
    assert not (project.root.parent / "escape.py").exists()


def test_edit_requires_a_unique_match(project):
    (project.root / "a.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    box = ToolBox("developer", project.root, RecordingExecutor(project.root))

    output, is_error = box.dispatch("edit_file",
                                    {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"})
    assert is_error and "appears 2 times" in output

    output, is_error = box.dispatch("edit_file",
                                    {"path": "a.py", "old_text": "nope", "new_text": "y"})
    assert is_error and "does not appear" in output


def test_commands_go_through_the_executor_not_the_host(project):
    executor = RecordingExecutor(project.root, results={"echo hi": ("hi\n", 0)})
    box = ToolBox("developer", project.root, executor, shell="sh")
    output, is_error = box.dispatch("run_command", {"command": "echo hi"})
    assert not is_error
    assert executor.commands == [["sh", "-c", "echo hi"]]
    assert "exit 0" in output and "hi" in output


# ------------------------------------------------------------ context index

@pytest.mark.skipif(not index_available(), reason="tree-sitter is not installed")
def test_index_answers_structure_questions_without_reading_files(tmp_path):
    (tmp_path / "lib.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from lib import helper\n\n\ndef main():\n    return helper()\n", encoding="utf-8")

    index = ContextIndex(tmp_path)
    stats = index.build()
    assert stats["indexed"] == 2

    assert [p for p, _ in index.definitions("helper")] == ["lib.py"]
    assert index.callers("helper") == ["app.py"]
    assert index.importers("lib") == ["app.py"]
    assert [s.name for s in index.outline("app.py")] == ["main"]


@pytest.mark.skipif(not index_available(), reason="tree-sitter is not installed")
def test_index_reuses_unchanged_files_and_notices_edits(project, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.py").write_text("def one():\n    pass\n", encoding="utf-8")

    index = ContextIndex(source)
    assert index.build()["indexed"] == 1
    assert index.build()["reused"] == 1

    (source / "a.py").write_text("def two():\n    pass\n", encoding="utf-8")
    assert index.build()["indexed"] == 1
    assert [s.name for s in index.outline("a.py")] == ["two"]


# -------------------------------------------------------------------- gates

def test_gate_result_must_satisfy_its_contract(project):
    executor = RecordingExecutor(project.root, results={
        "/g/build.sh": ('{"gate":"build","status":"pass","exit_code":0,'
                        '"summary":"build succeeded","command":"make"}\n', 0)})
    result = gates.run_gate("build", executor, ticket="T-001", gates_dir="/g",
                            report_dir="/r", project=project)
    assert result.passed and result.summary == "build succeeded"


def test_unparseable_gate_output_is_a_failure_not_a_pass(project):
    executor = RecordingExecutor(project.root, results={"/g/build.sh": ("chatter\n", 0)})
    result = gates.run_gate("build", executor, ticket="T-001", gates_dir="/g",
                            report_dir="/r", project=project)
    assert result.status == "fail" and "no parseable result" in result.summary


def test_gate_result_violating_its_contract_is_a_failure(project):
    executor = RecordingExecutor(project.root, results={
        "/g/build.sh": ('{"gate":"build","status":"maybe","exit_code":0,"summary":"x"}\n', 0)})
    result = gates.run_gate("build", executor, ticket="T-001", gates_dir="/g",
                            report_dir="/r", project=project)
    assert result.status == "fail" and "contract" in result.summary


def test_noise_on_stdout_cannot_corrupt_the_result(project):
    payload = '{"gate":"test","status":"pass","exit_code":0,"summary":"ok","command":"pytest"}'
    executor = RecordingExecutor(project.root, results={
        "/g/test.sh": (f"building...\n{{not json}}\n{payload}\n", 0)})
    result = gates.run_gate("test", executor, ticket="T-001", gates_dir="/g",
                            report_dir="/r", project=project)
    assert result.passed


def test_a_skipped_required_gate_is_not_a_pass(project):
    thresholds = Thresholds.load(project)
    results = [GateResult("build", "skip", 0, "no build system"),
               GateResult("test", "pass", 0, "ok"),
               GateResult("security-scan", "pass", 0, "clean")]
    passed, reasons = gates.verdict(results, thresholds)
    assert not passed
    assert any("required by thresholds.yaml but skipped" in r for r in reasons)


def test_a_skipped_optional_gate_is_tolerated(project):
    thresholds = Thresholds.load(project)
    results = [GateResult("build", "pass", 0, "ok"),
               GateResult("test", "pass", 0, "ok"),
               GateResult("security-scan", "pass", 0, "clean"),
               GateResult("lint", "skip", 0, "no linter")]
    passed, reasons = gates.verdict(results, thresholds)
    assert passed and reasons == []


def test_a_required_gate_that_never_ran_blocks(project):
    thresholds = Thresholds.load(project)
    passed, reasons = gates.verdict([GateResult("build", "pass", 0, "ok")], thresholds)
    assert not passed
    assert any("test: required but never ran" in r for r in reasons)


def test_gate_results_round_trip_to_disk(project):
    results = [GateResult("build", "pass", 0, "ok", metrics={"passed": 3})]
    gates.write_results(project, "T-001", results)
    loaded = gates.read_results(project, "T-001")
    assert loaded[0].gate == "build" and loaded[0].metrics == {"passed": 3}


@pytest.mark.skipif(posix_shell() is None, reason="no POSIX shell on this machine")
def test_gate_scripts_actually_run(project, tmp_path):
    """The scripts are the deterministic layer; run them for real."""
    workdir = tmp_path / "target"
    workdir.mkdir()
    (workdir / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n",
                                            encoding="utf-8")
    (workdir / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    executor = HostExecutor(root=workdir)
    gates_dir = project.resolve("gates").as_posix()
    reports = tmp_path / "reports" / "T-001"
    result = gates.run_gate("build", executor, ticket="T-001", gates_dir=gates_dir,
                            report_dir=reports.as_posix(),
                            project=project, shell=posix_shell())

    assert result.gate == "build"
    assert result.status == "pass", result.summary
    evidence = reports / "evidence" / "build.log"
    assert evidence.is_file() and "compileall" in evidence.read_text(encoding="utf-8")


@pytest.mark.skipif(posix_shell() is None, reason="no POSIX shell on this machine")
def test_build_gate_fails_on_broken_source(project, tmp_path):
    workdir = tmp_path / "broken"
    workdir.mkdir()
    (workdir / "requirements.txt").write_text("", encoding="utf-8")
    (workdir / "bad.py").write_text("def f(:\n", encoding="utf-8")

    result = gates.run_gate("build", HostExecutor(root=workdir), ticket="T-002",
                            gates_dir=project.resolve("gates").as_posix(),
                            report_dir=(tmp_path / "reports").as_posix(),
                            project=project, shell=posix_shell())
    assert result.status == "fail"


# ---------------------------------------------------------------- worktrees

@pytest.fixture
def git_project(tmp_path) -> Project:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "Test"]):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    (root / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "init"],
                   capture_output=True, check=True)
    p = Project(root)
    p.ensure_layout()
    return p


def test_each_ticket_gets_its_own_branch_and_tree(git_project):
    first = worktrees.ensure(git_project, "T-001")
    second = worktrees.ensure(git_project, "T-002")

    assert first.branch == "harness/T-001" and second.branch == "harness/T-002"
    assert first.path != second.path
    assert (first.path / "README.md").is_file()
    assert {w.ticket for w in worktrees.existing(git_project)} == {"T-001", "T-002"}


def test_ensure_reattaches_instead_of_restarting(git_project):
    worktree = worktrees.ensure(git_project, "T-001")
    (worktree.path / "work.py").write_text("in progress\n", encoding="utf-8")

    again = worktrees.ensure(git_project, "T-001")
    assert again.path == worktree.path
    assert (again.path / "work.py").is_file()  # work was not discarded


def test_worktrees_are_isolated_from_each_other(git_project):
    first = worktrees.ensure(git_project, "T-001")
    second = worktrees.ensure(git_project, "T-002")
    (first.path / "only-in-first.py").write_text("x\n", encoding="utf-8")
    assert not (second.path / "only-in-first.py").exists()


def test_commit_and_removal(git_project):
    worktree = worktrees.ensure(git_project, "T-001")
    assert worktrees.commit_all(worktree, "empty") is None

    (worktree.path / "new.py").write_text("x = 1\n", encoding="utf-8")
    sha = worktrees.commit_all(worktree, "T-001: work")
    assert sha and len(sha) == 40
    assert not worktrees.is_dirty(worktree)
    assert "new.py" in worktrees.diff(worktree, "main")

    assert worktrees.remove(git_project, "T-001") is True
    assert worktrees.existing(git_project) == []


def test_removal_refuses_to_discard_uncommitted_work(git_project):
    worktree = worktrees.ensure(git_project, "T-001")
    (worktree.path / "wip.py").write_text("unsaved\n", encoding="utf-8")
    with pytest.raises(worktrees.GitError, match="uncommitted"):
        worktrees.remove(git_project, "T-001")
    assert worktrees.remove(git_project, "T-001", force=True) is True


def test_a_non_repository_is_a_clean_failure(project):
    with pytest.raises(worktrees.GitError, match="not a git repository"):
        worktrees.ensure(project, "T-001")


def test_history_is_rendered_for_agents_that_cannot_run_git(git_project):
    worktree = worktrees.ensure(git_project, "T-001")
    (worktree.path / "new.py").write_text("x = 1\n", encoding="utf-8")
    worktrees.commit_all(worktree, "T-001: a distinctive subject line")

    rendered = worktrees.history(worktree.path)
    assert "a distinctive subject line" in rendered


def test_history_says_so_rather_than_returning_nothing(tmp_path):
    """An empty repository must not read as 'no conventions to follow'."""
    root = tmp_path / "fresh"
    root.mkdir()
    worktrees.git(root, "init", "-b", "main")

    rendered = worktrees.history(root)
    assert "no commit history yet" in rendered


def test_history_is_bounded(git_project):
    worktree = worktrees.ensure(git_project, "T-001")
    for n in range(12):
        (worktree.path / f"f{n}.py").write_text(f"x = {n}\n", encoding="utf-8")
        worktrees.commit_all(worktree, f"T-001: commit number {n}")

    assert len(worktrees.history(worktree.path, max_chars=200)) < 300
    assert len(worktrees.history(worktree.path, max_commits=3).splitlines()) == 3


# ---------------------------------------------------------------- pipeline

def _task_entry(status, depends_on=()):
    return {"status": status, "depends_on": list(depends_on)}


def test_only_dependency_satisfied_tickets_are_ready(project):
    write_task(project, "T-001", "backend")
    write_task(project, "T-002", "frontend", depends_on=["T-001"])
    write_task(project, "T-003", "frontend", depends_on=["T-001"])
    from ai_harness import taskfile
    tasks = taskfile.load_all(project)

    state = {"tasks": {"T-001": _task_entry("pending"),
                       "T-002": _task_entry("pending", ["T-001"]),
                       "T-003": _task_entry("pending", ["T-001"])}}
    assert [t.id for t in pipeline.ready_tickets(state, tasks)] == ["T-001"]

    state["tasks"]["T-001"] = _task_entry("done")
    assert [t.id for t in pipeline.ready_tickets(state, tasks)] == ["T-002", "T-003"]

    state["tasks"]["T-002"] = _task_entry("in_progress", ["T-001"])
    assert [t.id for t in pipeline.ready_tickets(state, tasks)] == ["T-003"]


def _pipeline(project, git_project=None):
    """A pipeline object wired far enough to exercise its pure parts."""
    write_task(project, "T-001", "backend")
    from ai_harness import taskfile
    task = taskfile.load(project.tasks / "backend" / "T-001.md", project)
    return pipeline.TicketPipeline(
        project, task, log=EventLog(project.events),
        executor=RecordingExecutor(project.root),
        worktree=worktrees.Worktree("T-001", project.root, "harness/T-001"),
        registry=Registry.load(project), thresholds=Thresholds.load(project),
        gates_dir="/g", report_dir="/r")


def _agent_result(agent, **fields):
    return AgentResult(agent=agent, model="claude-sonnet-5", effort="high",
                       result={"status": "complete", "blocking_reason": "",
                               "summary": "", "report_markdown": "", **fields})


def test_the_architect_is_handed_git_history_it_cannot_read_itself(project, monkeypatch):
    """Git is host-only, so the Architect's history has to arrive as context."""
    engine = _pipeline(project)
    monkeypatch.setattr(worktrees, "history", lambda *a, **k: "deadbee 2026-01-01 x: first")

    seen = {}

    def capture(agent, extra):
        seen[agent] = extra
        raise pipeline.PipelineError("stop after the architect")

    monkeypatch.setattr(engine, "_run_agent", capture)
    monkeypatch.setattr(engine, "build_index", lambda: None)

    with pytest.raises(pipeline.PipelineError):
        engine.run()

    assert seen["architect"]["git_history"] == "deadbee 2026-01-01 x: first"


def test_the_rendered_history_reaches_the_prompt(project):
    from ai_harness import taskfile
    write_task(project, "T-001", "backend")
    task = taskfile.load(project.tasks / "backend" / "T-001.md", project)
    worktree = worktrees.Worktree("T-001", project.root, "harness/T-001")

    prompt = pipeline._prompt_for("architect", task, worktree,
                                  {"git_history": "deadbee 2026-01-01 x: first"})
    assert "<git_history>" in prompt and "deadbee" in prompt


def test_a_failing_gate_blocks_approval(project):
    engine = _pipeline(project)
    blocking = engine._blocking_conditions(False, ["build: failed"], {})
    assert blocking == ["gate: build: failed"]


def test_a_high_security_finding_blocks_approval(project):
    engine = _pipeline(project)
    reviews = {"security": _agent_result("security", findings=[
        {"title": "Missing authz", "severity": "HIGH", "location": "a.py:1",
         "evidence": "e", "recommendation": "r", "verification": "v"}])}
    blocking = engine._blocking_conditions(True, [], reviews)
    assert blocking == ["security HIGH: Missing authz"]


def test_a_medium_security_finding_does_not_block(project):
    engine = _pipeline(project)
    reviews = {"security": _agent_result("security", findings=[
        {"title": "Verbose error", "severity": "MEDIUM", "location": "a.py:1",
         "evidence": "e", "recommendation": "r", "verification": "v"}])}
    assert engine._blocking_conditions(True, [], reviews) == []


def test_failing_tests_block_approval(project):
    engine = _pipeline(project)
    reviews = {"qa": _agent_result("qa", tests_failed=3, findings=[])}
    assert engine._blocking_conditions(True, [], reviews) == ["qa: 3 test(s) failing"]


def test_adr_numbers_are_allocated_not_chosen(project):
    engine = _pipeline(project)
    engine._write_adr("# ADR\n\nUse Postgres.")
    engine._write_adr("# ADR\n\nUse Redis.")
    written = sorted(p.name for p in project.architecture.glob("ADR-*.md"))
    assert written == ["ADR-001.md", "ADR-002.md"]

    (project.architecture / "ADR-009.md").write_text("x", encoding="utf-8")
    engine._write_adr("# ADR\n\nThird.")
    assert (project.architecture / "ADR-010.md").is_file()


def test_agent_events_go_to_the_tickets_own_stream(project):
    engine = _pipeline(project)
    engine._status("in_progress")
    log = EventLog(project.events)
    assert log.streams() == ["T-001"]
    assert fold(log)["tasks"]["T-001"]["status"] == "in_progress"


# ----------------------------------------------------------- approval gate

def _approved_ticket(project, *, gate_status="pass", reports=("security", "qa", "reviewer")):
    log = EventLog(project.events)
    log.append("tasks.generated", {"tasks": [{"id": "T-001", "dir": "backend",
                                              "depends_on": []}]})
    log.append("task.review_decided", {"task_id": "T-001", "decision": "APPROVE"},
               stream="T-001")
    log.append("task.human_approval_requested", {"task_id": "T-001"}, stream="T-001")

    gates.write_results(project, "T-001", [
        GateResult("build", gate_status, 0 if gate_status == "pass" else 1, "build"),
        GateResult("test", "pass", 0, "3 passed"),
    ])
    directory = project.reports / "T-001"
    directory.mkdir(parents=True, exist_ok=True)
    for agent in reports:
        (directory / agents.REPORT_FILES[agent]).write_text(
            f"# {agent}\n\nEvidence for {agent}.\n", encoding="utf-8")
    return fold(log)


def test_the_digest_carries_the_real_evidence(project):
    state = _approved_ticket(project)
    digest, problems = approve.build_digest(project, "T-001", state)
    assert problems == []
    assert "Evidence for security" in digest
    assert "Evidence for qa" in digest
    assert "Evidence for reviewer" in digest
    assert "3 passed" in digest


def test_approval_is_refused_when_a_report_is_missing(project):
    state = _approved_ticket(project, reports=("security", "qa"))
    _, problems = approve.build_digest(project, "T-001", state)
    assert any("reviewer report is missing" in p for p in problems)


def test_approval_is_refused_when_a_gate_failed(project):
    state = _approved_ticket(project, gate_status="fail")
    _, problems = approve.build_digest(project, "T-001", state)
    assert any("gate build failed" in p for p in problems)


def test_approval_is_refused_before_the_reviewer_approves(project):
    log = EventLog(project.events)
    log.append("tasks.generated", {"tasks": [{"id": "T-001", "dir": "backend",
                                              "depends_on": []}]})
    state = fold(log)
    _, problems = approve.build_digest(project, "T-001", state)
    assert any("not review_approved" in p for p in problems)


def test_approving_twice_is_refused(project):
    _approved_ticket(project)
    log = EventLog(project.events)
    log.append("task.human_approved", {"task_id": "T-001"}, stream="T-001")
    _, problems = approve.build_digest(project, "T-001", fold(log))
    assert any("already been approved" in p for p in problems)


def test_the_gate_writes_the_digest_and_refuses_cleanly(project, monkeypatch, capsys):
    _approved_ticket(project, gate_status="fail")
    monkeypatch.chdir(project.root)
    args = type("A", (), {"ticket": "T-001", "show": False, "no_pr": True})()

    with pytest.raises(CommandError, match="cannot be approved"):
        approve.run(args)

    digest = project.reports / "T-001" / approve.DIGEST_FILE
    assert digest.is_file() and "Approval evidence" in digest.read_text(encoding="utf-8")


def test_show_prints_without_asking(project, monkeypatch, capsys):
    _approved_ticket(project)
    monkeypatch.chdir(project.root)
    args = type("A", (), {"ticket": "T-001", "show": True, "no_pr": True})()

    assert approve.run(args) == 0
    assert "Evidence for reviewer" in capsys.readouterr().out
    assert fold(EventLog(project.events))["tasks"]["T-001"]["status"] != "human_approved"


def test_declining_the_gate_changes_nothing(project, monkeypatch):
    _approved_ticket(project)
    monkeypatch.chdir(project.root)
    monkeypatch.setattr(approve, "confirm", lambda prompt: False)
    args = type("A", (), {"ticket": "T-001", "show": False, "no_pr": True})()

    assert approve.run(args) == 1
    assert fold(EventLog(project.events))["tasks"]["T-001"]["status"] != "human_approved"


def test_approving_records_it_in_the_log(project, monkeypatch):
    _approved_ticket(project)
    monkeypatch.chdir(project.root)
    monkeypatch.setattr(approve, "confirm", lambda prompt: True)
    args = type("A", (), {"ticket": "T-001", "show": False, "no_pr": True})()

    assert approve.run(args) == 0
    state = fold(EventLog(project.events))
    assert state["tasks"]["T-001"]["status"] == "human_approved"
    assert state["human_approvals_pending"] == []


# ------------------------------------------------- reaching the worktree

def _ticket_branch_with_work(project: Project, ticket: str = "T-001"):
    """A ticket whose work is committed on its branch, as after a real run."""
    worktree = worktrees.ensure(project, ticket)
    (worktree.path / "feature.py").write_text("shipped\n", encoding="utf-8")
    worktrees.commit_all(worktree, f"{ticket}: work")
    return worktree


def test_the_gate_recreates_a_worktree_that_run_removed(git_project):
    """`harness run` deletes the tree unless --keep-worktree; the work survives
    on the branch. The gate reconstructs it rather than requiring the user to
    have anticipated this two commands earlier."""
    _ticket_branch_with_work(git_project)
    worktrees.remove(git_project, "T-001")
    assert worktrees.existing(git_project) == []

    reached, borrowed = approve._reach_worktree(git_project, "T-001")
    assert borrowed is True
    assert (reached.path / "feature.py").is_file()


def test_a_worktree_that_is_already_there_is_not_borrowed(git_project):
    worktree = _ticket_branch_with_work(git_project)
    reached, borrowed = approve._reach_worktree(git_project, "T-001")
    assert borrowed is False and reached.path == worktree.path


def test_no_branch_at_all_is_a_clean_skip(git_project):
    reached, borrowed = approve._reach_worktree(git_project, "T-404")
    assert reached is None and borrowed is False


def test_a_borrowed_worktree_is_put_back(git_project, monkeypatch):
    """The gate should leave the repository as it found it, or the next `run`
    inherits trees nobody asked for."""
    _ticket_branch_with_work(git_project)
    worktrees.remove(git_project, "T-001")

    seen = {}
    monkeypatch.setattr(approve, "_offer_pull_request",
                        lambda project, ticket, worktree: seen.update(branch=worktree.branch))
    approve._post_approval(git_project, "T-001", type("A", (), {"no_pr": False})())

    assert seen["branch"] == "harness/T-001"
    assert worktrees.existing(git_project) == []


def test_no_pr_never_touches_the_worktree(git_project, monkeypatch):
    worktree = _ticket_branch_with_work(git_project)
    monkeypatch.setattr(approve, "_reach_worktree",
                        lambda *a: pytest.fail("should not have been called"))
    approve._post_approval(git_project, "T-001", type("A", (), {"no_pr": True})())
    assert worktree.path.is_dir()


# ------------------------------------------------------------- thresholds

def test_thresholds_come_from_content_and_are_overridable(project):
    assert Thresholds.load(project).max_block_cycles == 3
    assert Thresholds.load(project).fail_on_missing_scanner is True

    override = project.harness / "overrides" / "gates"
    override.mkdir(parents=True, exist_ok=True)
    (override / "thresholds.yaml").write_text(
        "review:\n  max_block_cycles: 1\ngates:\n  required: [test]\n  optional: []\n",
        encoding="utf-8")
    thresholds = Thresholds.load(project)
    assert thresholds.max_block_cycles == 1
    assert thresholds.required_gates == ["test"]


# ---------------------------------------------------------------- contracts

def test_phase3_events_satisfy_the_event_contract(project):
    from ai_harness import contracts
    log = EventLog(project.events)
    for type_, payload in [
        ("ticket.worktree_created", {"task_id": "T-001", "branch": "harness/T-001"}),
        ("ticket.isolation_waived", {"task_id": "T-001", "reason": "--no-container"}),
        ("ticket.gates_ran", {"task_id": "T-001", "passed": True}),
        ("ticket.committed", {"task_id": "T-001", "sha": "abc"}),
        ("ticket.adr_written", {"task_id": "T-001", "number": 1}),
        ("task.agent_started", {"task_id": "T-001", "agent": "developer"}),
        ("task.review_decided", {"task_id": "T-001", "decision": "APPROVE"}),
        ("task.pr_opened", {"task_id": "T-001", "number": 4}),
    ]:
        log.append(type_, payload, stream="T-001")

    for line in (project.events / "T-001.jsonl").read_text(encoding="utf-8").splitlines():
        contracts.validate("event", json.loads(line), project)
