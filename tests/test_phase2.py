"""Tests for phase 2 — setup. No network: the tracker adapter is mocked at the
seam the spec asked for, which is the point of having the seam."""

from __future__ import annotations

import dataclasses
import json
import os

import pytest

from ai_harness import cli, contracts, env, taskfile, trackers
from ai_harness.commands import setup_project
from ai_harness.commands._common import CommandError
from ai_harness.events import EventLog
from ai_harness.paths import Project
from ai_harness.state import fold
from ai_harness.taskfile import TaskFileError
from ai_harness.trackers.base import (Issue, IssueSpec, RepoRef, Tracker,
                                      TrackerItemError, TrackerSystemicError)
from ai_harness.trackers.github import parse_remote, parse_repo_arg

TASK_BODY = """\
## Context

Users cannot sign in.

## Task

Build the login form.

## Acceptance criteria

- [ ] Form validates email format client-side
"""


def write_task(project: Project, task_id: str, directory: str = "frontend",
               phase: int = 1, depends_on: list[str] | None = None,
               title: str = "Implement login form", **extra) -> None:
    frontmatter = {
        "id": task_id, "title": title, "phase": phase, "dir": directory,
        "depends_on": depends_on or [], "inputs": ["revised-spec.md#section-3.2"],
        "start_condition": "auth API contract is finalised",
        "done_condition": "Login form renders and passes QA",
        "acceptance_criteria": ["Form validates email format client-side"],
        "status": "pending",
    }
    frontmatter.update(extra)
    target = project.tasks / directory
    target.mkdir(parents=True, exist_ok=True)
    task = taskfile.TaskFile(target / f"{task_id}.md", frontmatter, TASK_BODY)
    task.save()


@pytest.fixture
def project(tmp_path) -> Project:
    p = Project(tmp_path)
    p.ensure_layout()
    return p


@pytest.fixture
def approved(project) -> Project:
    """A project that has cleared phase 1: spec approved, tasks on disk."""
    log = EventLog(project.events)
    log.append("harness.initialized", {})
    log.append("spec.finalized", {"forced": False})
    write_task(project, "T-001", "backend", phase=1, title="Auth API")
    write_task(project, "T-002", "frontend", phase=1, depends_on=["T-001"])
    write_task(project, "T-003", "frontend", phase=2, depends_on=["T-002"],
               title="Password reset")
    log.append("tasks.generated", {"tasks": [
        {"id": "T-001", "dir": "backend", "phase": 1, "title": "Auth API", "depends_on": []},
        {"id": "T-002", "dir": "frontend", "phase": 1, "title": "Implement login form",
         "depends_on": ["T-001"]},
        {"id": "T-003", "dir": "frontend", "phase": 2, "title": "Password reset",
         "depends_on": ["T-002"]},
    ]})
    return project


class FakeTracker(Tracker):
    """Stands in for GitHub. Records every outward call so tests can assert on
    what would have been sent."""

    provider = "github"

    def __init__(self, *, existing_repos=("acme/app",), item_errors=None,
                 systemic_errors=None):
        self._repos = {r: f"https://github.example/{r}" for r in existing_repos}
        self._labels: dict[str, dict[str, tuple[str, str]]] = {}
        self._milestones: dict[str, dict[str, int]] = {}
        self.issues: dict[int, Issue] = {}
        self._next = 1
        self.item_errors = set(item_errors or ())
        self.systemic_errors = set(systemic_errors or ())
        self.created_repos: list[tuple[str, bool]] = []
        self.updated: list[tuple[int, IssueSpec]] = []
        self.pull_requests: list[dict] = []

    def whoami(self) -> str:
        return "tester"

    def create_repo(self, name, *, owner=None, private=True):
        full = f"{owner}/{name}"
        self.created_repos.append((full, private))
        self._repos[full] = f"https://github.example/{full}"
        return RepoRef(owner, name), self._repos[full]

    def repo_url(self, repo):
        return self._repos.get(repo.full)

    def labels(self, repo):
        return set(self._labels.setdefault(repo.full, {}))

    def milestones(self, repo):
        return dict(self._milestones.setdefault(repo.full, {}))

    def ensure_label(self, repo, name, color, description):
        known = self._labels.setdefault(repo.full, {})
        if name in known:
            return False
        known[name] = (color, description)
        return True

    def ensure_milestone(self, repo, title, description):
        known = self._milestones.setdefault(repo.full, {})
        if title in known:
            return known[title], False
        known[title] = len(known) + 1
        return known[title], True

    def _guard(self, spec: IssueSpec) -> None:
        task_id = spec.title.split(":")[0]
        if task_id in self.systemic_errors:
            raise TrackerSystemicError("rate limit reached")
        if task_id in self.item_errors:
            raise TrackerItemError("validation failed")

    def create_issue(self, repo, spec):
        self._guard(spec)
        number = self._next
        self._next += 1
        issue = Issue(number=number, title=spec.title, body=spec.body,
                      labels=tuple(sorted(spec.labels)), milestone=spec.milestone,
                      url=f"https://github.example/{repo.full}/issues/{number}")
        self.issues[number] = issue
        return issue

    def get_issue(self, repo, number):
        return self.issues.get(number)

    def open_pull_request(self, repo, *, head, base, title, body, draft=True):
        number = self._next
        self._next += 1
        self.pull_requests.append({"repo": repo.full, "head": head, "base": base,
                                   "title": title, "body": body, "draft": draft})
        return number, f"https://github.example/{repo.full}/pull/{number}"

    def default_branch(self, repo):
        return "main"

    def update_issue(self, repo, number, spec):
        self._guard(spec)
        old = self.issues[number]
        issue = Issue(number=number, title=spec.title, body=spec.body,
                      labels=tuple(sorted(spec.labels)),
                      milestone=spec.milestone or old.milestone, url=old.url)
        self.issues[number] = issue
        self.updated.append((number, spec))
        return issue


def run_setup(monkeypatch, project, tracker, *argv):
    monkeypatch.chdir(project.root)
    monkeypatch.setattr(trackers, "get", lambda provider, proj=None: (tracker, "test"))
    args = cli.build_parser().parse_args(["setup-project", *argv])
    return setup_project.run(args)


# ------------------------------------------------------------------- dotenv

def test_dotenv_parses_the_forms_credentials_actually_use():
    parsed = env.parse(
        "# comment\n"
        "\n"
        "GITHUB_TOKEN=ghp_plain\n"
        "export JIRA_TOKEN='single quoted'\n"
        'OTHER="double quoted"\n'
        "TRAILING=value # not part of the value\n"
        "malformed line\n"
    )
    assert parsed == {
        "GITHUB_TOKEN": "ghp_plain", "JIRA_TOKEN": "single quoted",
        "OTHER": "double quoted", "TRAILING": "value",
    }


def test_credential_precedence_is_env_then_project_then_user(project, tmp_path, monkeypatch):
    user_home = tmp_path / "userhome"
    user_home.mkdir()
    monkeypatch.setenv("AI_HARNESS_HOME", str(user_home))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    (user_home / ".env").write_text("GITHUB_TOKEN=user_level\n", encoding="utf-8")
    assert env.lookup("GITHUB_TOKEN", project)[0] == "user_level"

    (project.root / ".env").write_text("GITHUB_TOKEN=project_level\n", encoding="utf-8")
    assert env.lookup("GITHUB_TOKEN", project)[0] == "project_level"

    monkeypatch.setenv("GITHUB_TOKEN", "process_level")
    value, source = env.lookup("GITHUB_TOKEN", project)
    assert (value, source) == ("process_level", "environment")


def test_missing_credential_is_none_not_an_exception(project, tmp_path, monkeypatch):
    monkeypatch.setenv("AI_HARNESS_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert env.lookup("GITHUB_TOKEN", project) is None


def test_env_credentials_are_loaded_into_the_environment(project, monkeypatch):
    # The Anthropic SDK reads the key from os.environ, so a key that only lives
    # in the repo's .env has to be copied there or the API runner never sees it.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (project.root / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-ant-from-dotenv\n", encoding="utf-8")

    loaded = env.load_into_environ(project)

    assert loaded == {"ANTHROPIC_API_KEY": str(project.root / ".env")}
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-dotenv"


def test_a_real_env_var_is_not_overwritten_by_dotenv(project, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-env")
    (project.root / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-ant-from-dotenv\n", encoding="utf-8")

    loaded = env.load_into_environ(project)

    assert loaded == {}  # nothing loaded from a file
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-real-env"


# ------------------------------------------------------------ repo resolution

@pytest.mark.parametrize("url", [
    "git@github.com:acme/app.git",
    "https://github.com/acme/app.git",
    "https://github.com/acme/app",
    "https://user@github.com/acme/app.git",
    "ssh://git@github.com/acme/app.git",
])
def test_every_git_remote_form_resolves(url):
    assert parse_remote(url) == RepoRef("acme", "app")


def test_non_github_remote_is_not_guessed_at():
    assert parse_remote("git@gitlab.com:acme/app.git") is None
    assert parse_repo_arg("app") is None


def test_repo_is_read_from_git_config(project):
    git_dir = project.root / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        '[core]\n\trepositoryformatversion = 0\n'
        '[remote "origin"]\n'
        '\turl = git@github.com:acme/app.git\n'
        '\tfetch = +refs/heads/*:refs/remotes/origin/*\n',
        encoding="utf-8")
    assert setup_project._repo_from_git_config(project) == RepoRef("acme", "app")


def test_missing_git_config_resolves_to_nothing(project):
    assert setup_project._repo_from_git_config(project) is None


def test_issue_ref_round_trips():
    assert setup_project._parse_issue_ref("github:acme/app#42") == (
        "github", RepoRef("acme", "app"), 42)
    assert setup_project._parse_issue_ref("nonsense") is None


# ---------------------------------------------------------------- task files

def test_task_file_round_trips_without_reordering(project):
    write_task(project, "T-001")
    path = project.tasks / "frontend" / "T-001.md"
    before = path.read_text(encoding="utf-8")

    task = taskfile.load(path, project)
    task.save()
    assert path.read_text(encoding="utf-8") == before


def test_recording_an_issue_ref_is_a_one_line_diff(project):
    write_task(project, "T-001")
    path = project.tasks / "frontend" / "T-001.md"
    before = path.read_text(encoding="utf-8").splitlines()

    task = taskfile.load(path, project)
    setup_project._write_issue_ref(task, "github:acme/app#7", project)

    after = path.read_text(encoding="utf-8").splitlines()
    assert [line for line in after if line not in before] == ["issue_ref: github:acme/app#7"]
    assert taskfile.load(path, project).issue_ref == "github:acme/app#7"


def test_task_contract_accepts_a_recorded_issue_ref(project):
    task = taskfile.TaskFile(project.tasks / "T-001.md", {
        "id": "T-001", "title": "t", "phase": 1, "dir": "frontend", "depends_on": [],
        "inputs": [], "start_condition": "a", "done_condition": "b",
        "acceptance_criteria": ["c"], "status": "pending",
        "issue_ref": "github:acme/app#1",
    }, TASK_BODY)
    contracts.validate("task", task.frontmatter, project)

    task.frontmatter["issue_ref"] = "not a reference"
    with pytest.raises(contracts.ContractViolation):
        contracts.validate("task", task.frontmatter, project)


def test_hand_edited_task_file_fails_loudly(project):
    write_task(project, "T-001")
    path = project.tasks / "frontend" / "T-001.md"
    path.write_text(path.read_text(encoding="utf-8").replace("phase: 1", "phase: nope"),
                    encoding="utf-8")
    with pytest.raises(TaskFileError, match="contract"):
        taskfile.load_all(project)


def test_sections_are_extracted_from_the_body(project):
    write_task(project, "T-001")
    task = taskfile.load(project.tasks / "frontend" / "T-001.md", project)
    assert task.section("Context") == "Users cannot sign in."
    assert task.section("Task") == "Build the login form."
    assert task.section("Nonexistent") == ""


# ------------------------------------------------------------------ planning

def test_issues_are_ordered_so_dependencies_exist_first(project):
    write_task(project, "T-003", depends_on=["T-002"])
    write_task(project, "T-002", depends_on=["T-001"])
    write_task(project, "T-001")
    ordered = setup_project._topological_order(taskfile.load_all(project))
    assert [t.id for t in ordered] == ["T-001", "T-002", "T-003"]


def test_label_colour_is_stable_per_directory():
    assert setup_project._label_color("frontend") == setup_project._label_color("frontend")
    assert setup_project._label_color("frontend") != setup_project._label_color("backend")
    assert len(setup_project._label_color("frontend")) == 6


def test_issue_body_links_back_and_embeds_the_checklist(project):
    write_task(project, "T-002", depends_on=["T-001"])
    task = taskfile.load(project.tasks / "frontend" / "T-002.md", project)
    body = setup_project._issue_body(task, project, {"T-001": 7})

    assert ".harness/tasks/frontend/T-002.md" in body
    assert "- [ ] Form validates email format client-side" in body
    assert "- [ ] #7 (T-001)" in body
    assert "Users cannot sign in." in body
    assert "\\" not in body  # the task-file link stays a URL, not a Windows path


def test_drift_ignores_line_endings_and_extra_human_labels(project):
    write_task(project, "T-001")
    task = taskfile.load(project.tasks / "frontend" / "T-001.md", project)
    spec = IssueSpec(title=setup_project._issue_title(task),
                     body=setup_project._issue_body(task, project, {}),
                     labels=["frontend"], milestone="Phase 1")
    issue = Issue(number=1, title=spec.title, body=spec.body.replace("\n", "\r\n"),
                  labels=("frontend", "good first issue"), milestone="Phase 1", url="")
    assert setup_project._drift(issue, spec) == []
    assert setup_project._drift(dataclasses.replace(issue, title="renamed"), spec) == ["title"]


def test_drift_reports_each_field_that_changed():
    spec = IssueSpec(title="T-001: new title", body="new body",
                     labels=["frontend"], milestone="Phase 2")
    issue = Issue(number=1, title="T-001: old title", body="old body",
                  labels=("backend",), milestone="Phase 1", url="")
    assert setup_project._drift(issue, spec) == ["title", "body", "labels", "milestone"]


# -------------------------------------------------------------- preconditions

def test_setup_refuses_before_the_spec_is_approved(project, monkeypatch):
    EventLog(project.events).append("harness.initialized", {})
    write_task(project, "T-001")
    with pytest.raises(CommandError, match="not approved"):
        run_setup(monkeypatch, project, FakeTracker(), "existing", "--repo", "acme/app", "--yes")


def test_setup_refuses_without_task_files(project, monkeypatch):
    log = EventLog(project.events)
    log.append("harness.initialized", {})
    log.append("spec.finalized", {"forced": False})
    with pytest.raises(CommandError, match="no task files"):
        run_setup(monkeypatch, project, FakeTracker(), "existing", "--repo", "acme/app", "--yes")


def test_new_refuses_without_a_name_or_visibility(approved, monkeypatch):
    with pytest.raises(CommandError, match="repository name"):
        run_setup(monkeypatch, approved, FakeTracker(), "new", "--private", "--yes")
    with pytest.raises(CommandError, match="visibility"):
        run_setup(monkeypatch, approved, FakeTracker(), "new", "--repo", "app", "--yes")


def test_existing_refuses_visibility_flags(approved, monkeypatch):
    with pytest.raises(CommandError, match="only applies to"):
        run_setup(monkeypatch, approved, FakeTracker(), "existing",
                  "--repo", "acme/app", "--private", "--yes")


def test_unknown_repo_is_a_clean_failure(approved, monkeypatch):
    with pytest.raises(CommandError, match="does not exist"):
        run_setup(monkeypatch, approved, FakeTracker(), "existing",
                  "--repo", "acme/missing", "--yes")


def test_dry_run_sends_nothing(approved, monkeypatch, capsys):
    tracker = FakeTracker()
    assert run_setup(monkeypatch, approved, tracker, "existing",
                     "--repo", "acme/app", "--dry-run") == 0
    assert tracker.issues == {}
    assert "nothing sent to the tracker" in capsys.readouterr().out
    assert taskfile.load_all(approved)[0].issue_ref is None


def test_declining_the_prompt_sends_nothing(approved, monkeypatch):
    tracker = FakeTracker()
    monkeypatch.setattr(setup_project, "confirm", lambda prompt: False)
    assert run_setup(monkeypatch, approved, tracker, "existing", "--repo", "acme/app") == 1
    assert tracker.issues == {}


# ------------------------------------------------------------------ applying

def test_full_run_creates_issues_labels_and_milestones(approved, monkeypatch):
    tracker = FakeTracker()
    assert run_setup(monkeypatch, approved, tracker, "existing",
                     "--repo", "acme/app", "--yes") == 0

    assert len(tracker.issues) == 3
    assert tracker.labels(RepoRef("acme", "app")) == {"backend", "frontend"}
    assert set(tracker.milestones(RepoRef("acme", "app"))) == {"Phase 1", "Phase 2"}

    first = tracker.issues[1]
    assert first.title == "T-001: Auth API"
    assert first.labels == ("backend",)
    assert first.milestone == "Phase 1"

    # T-002 depends on T-001, which was created first and so has a number to link.
    assert "- [ ] #1 (T-001)" in tracker.issues[2].body


def test_run_records_links_in_both_the_log_and_the_task_files(approved, monkeypatch):
    tracker = FakeTracker()
    run_setup(monkeypatch, approved, tracker, "existing", "--repo", "acme/app", "--yes")

    state = fold(EventLog(approved.events))
    assert state["tasks"]["T-001"]["issue_ref"] == "github:acme/app#1"
    assert state["repo"] == {"provider": "github", "full": "acme/app",
                             "url": "https://github.example/acme/app", "created": False}
    assert state["phase"] == "execution"

    refs = {t.id: t.issue_ref for t in taskfile.load_all(approved)}
    assert refs == {"T-001": "github:acme/app#1", "T-002": "github:acme/app#2",
                    "T-003": "github:acme/app#3"}

    contracts.validate("state", {k: v for k, v in state.items()
                                 if not k.startswith("_")}, approved)
    for line in (approved.events / "main.jsonl").read_text(encoding="utf-8").splitlines():
        contracts.validate("event", json.loads(line), approved)


def test_rerun_creates_nothing_and_changes_nothing(approved, monkeypatch, capsys):
    tracker = FakeTracker()
    run_setup(monkeypatch, approved, tracker, "existing", "--repo", "acme/app", "--yes")
    capsys.readouterr()

    assert run_setup(monkeypatch, approved, tracker, "existing",
                     "--repo", "acme/app", "--yes") == 0
    assert len(tracker.issues) == 3
    assert tracker.updated == []
    assert "0 created, 0 updated, 3 already current" in capsys.readouterr().out


def test_rerun_reconciles_a_task_file_that_changed(approved, monkeypatch):
    tracker = FakeTracker()
    run_setup(monkeypatch, approved, tracker, "existing", "--repo", "acme/app", "--yes")

    path = approved.tasks / "backend" / "T-001.md"
    task = taskfile.load(path, approved)
    task.frontmatter["title"] = "Auth API, revised"
    task.frontmatter["acceptance_criteria"].append("Refresh tokens rotate")
    task.save()

    assert run_setup(monkeypatch, approved, tracker, "existing",
                     "--repo", "acme/app", "--yes") == 0
    assert tracker.issues[1].title == "T-001: Auth API, revised"
    assert "- [ ] Refresh tokens rotate" in tracker.issues[1].body
    assert [n for n, _ in tracker.updated] == [1]

    reconciled = [e for e in EventLog(approved.events).read_all()
                  if e.type == "task.issue_reconciled"]
    assert reconciled[0].payload["changes"] == ["title", "body"]


def test_reconciliation_keeps_labels_a_human_added(approved, monkeypatch):
    tracker = FakeTracker()
    run_setup(monkeypatch, approved, tracker, "existing", "--repo", "acme/app", "--yes")

    issue = tracker.issues[1]
    tracker.issues[1] = Issue(issue.number, "drifted", issue.body,
                              issue.labels + ("needs-triage",), issue.milestone, issue.url)

    run_setup(monkeypatch, approved, tracker, "existing", "--repo", "acme/app", "--yes")
    assert "needs-triage" in tracker.issues[1].labels
    assert "backend" in tracker.issues[1].labels


def test_a_task_file_carrying_a_ref_is_never_duplicated(approved, monkeypatch):
    """The event log may be gone; the task file alone must still prevent a duplicate."""
    tracker = FakeTracker()
    tracker.issues[99] = Issue(99, "T-001: Auth API", "stale", ("backend",), "Phase 1", "")
    path = approved.tasks / "backend" / "T-001.md"
    task = taskfile.load(path, approved)
    task.frontmatter["issue_ref"] = "github:acme/app#99"
    task.save()

    run_setup(monkeypatch, approved, tracker, "existing", "--repo", "acme/app", "--yes")
    assert sorted(tracker.issues) == [1, 2, 99]  # T-002 and T-003 only
    assert tracker.issues[99].title == "T-001: Auth API"


def test_new_creates_the_repo_and_no_directories(approved, monkeypatch):
    tracker = FakeTracker(existing_repos=())
    before = {p.name for p in approved.root.iterdir()}

    assert run_setup(monkeypatch, approved, tracker, "new", "--repo", "fresh",
                     "--public", "--yes") == 0
    assert tracker.created_repos == [("tester/fresh", False)]
    assert len(tracker.issues) == 3
    assert {p.name for p in approved.root.iterdir()} == before

    state = fold(EventLog(approved.events))
    assert state["repo"]["created"] is True
    assert state["repo"]["full"] == "tester/fresh"


def test_new_respects_an_explicit_owner(approved, monkeypatch):
    tracker = FakeTracker(existing_repos=())
    run_setup(monkeypatch, approved, tracker, "new", "--repo", "fresh",
              "--owner", "acme-org", "--private", "--yes")
    assert tracker.created_repos == [("acme-org/fresh", True)]


# ------------------------------------------------------------------ failures

def test_one_bad_issue_does_not_stop_the_others(approved, monkeypatch, capsys):
    tracker = FakeTracker(item_errors={"T-002"})
    assert run_setup(monkeypatch, approved, tracker, "existing",
                     "--repo", "acme/app", "--yes") == 1

    out = capsys.readouterr().out
    assert "2 created" in out and "1 task(s) failed" in out
    assert {i.title.split(":")[0] for i in tracker.issues.values()} == {"T-001", "T-003"}

    # The failure must not have advanced the phase or invented a link.
    state = fold(EventLog(approved.events))
    assert state["phase"] == "setup"
    assert state["tasks"]["T-002"]["issue_ref"] is None


def test_a_systemic_failure_aborts_but_keeps_what_was_created(approved, monkeypatch):
    tracker = FakeTracker(systemic_errors={"T-002"})
    with pytest.raises(CommandError, match="remaining task"):
        run_setup(monkeypatch, approved, tracker, "existing", "--repo", "acme/app", "--yes")

    assert len(tracker.issues) == 1
    state = fold(EventLog(approved.events))
    assert state["tasks"]["T-001"]["issue_ref"] == "github:acme/app#1"
    assert state["tasks"]["T-003"]["issue_ref"] is None
    assert state["phase"] == "setup"


def test_an_aborted_run_resumes_where_it_stopped(approved, monkeypatch):
    tracker = FakeTracker(systemic_errors={"T-002"})
    with pytest.raises(CommandError):
        run_setup(monkeypatch, approved, tracker, "existing", "--repo", "acme/app", "--yes")

    tracker.systemic_errors.clear()
    assert run_setup(monkeypatch, approved, tracker, "existing",
                     "--repo", "acme/app", "--yes") == 0
    assert len(tracker.issues) == 3  # T-001 was not created twice
    assert fold(EventLog(approved.events))["phase"] == "execution"


def test_a_deleted_issue_is_reported_not_silently_recreated(approved, monkeypatch, capsys):
    tracker = FakeTracker()
    run_setup(monkeypatch, approved, tracker, "existing", "--repo", "acme/app", "--yes")
    del tracker.issues[1]
    capsys.readouterr()

    assert run_setup(monkeypatch, approved, tracker, "existing",
                     "--repo", "acme/app", "--yes") == 1
    assert 1 not in tracker.issues
    assert "no longer exists" in capsys.readouterr().out


def test_an_issue_linked_to_another_repo_is_refused(approved, monkeypatch, capsys):
    task = taskfile.load(approved.tasks / "backend" / "T-001.md", approved)
    task.frontmatter["issue_ref"] = "github:someone/else#5"
    task.save()

    assert run_setup(monkeypatch, approved, FakeTracker(), "existing",
                     "--repo", "acme/app", "--yes") == 1
    assert "different repository" in capsys.readouterr().out
