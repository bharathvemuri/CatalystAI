"""Issue-tracker adapter interface.

Spec section 4 names GitHub but requires the seam so Jira or Linear can drop in
later. The seam is only real if the layer above never handles a GitHub concept,
so everything crossing this boundary is expressed in harness terms: a task's
``dir`` is a *label*, its ``phase`` is a *milestone*, and an issue is named by
the provider-qualified ``issue_ref`` the state contract already stores
(``github:org/repo#42``).

Failures split into two kinds because the command's recovery differs. A systemic
failure — rejected credential, missing permission, rate limit, unreachable host
— will fail identically for every remaining task, so continuing only manufactures
noise and the run aborts. An item failure is local to one issue, and the other
thirty-nine tasks have no reason to be punished for it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class TrackerError(RuntimeError):
    """Base for every adapter failure."""


class TrackerSystemicError(TrackerError):
    """The next call would fail the same way. Abort the run."""


class TrackerItemError(TrackerError):
    """One item was rejected. Record it and carry on."""


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def full(self) -> str:
        return f"{self.owner}/{self.name}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.full


@dataclass(frozen=True)
class IssueSpec:
    """What the harness wants an issue to say, in provider-neutral terms."""

    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    milestone: str | None = None


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    milestone: str | None
    url: str


class Tracker(ABC):
    """One issue tracker. Instances are bound to a credential, not to a repo."""

    provider: str

    def issue_ref(self, repo: RepoRef, number: int) -> str:
        """The identifier the state contract stores, e.g. ``github:org/repo#42``."""
        return f"{self.provider}:{repo.full}#{number}"

    @abstractmethod
    def whoami(self) -> str:
        """Account the credential authenticates as. Doubles as a credential check."""

    @abstractmethod
    def create_repo(self, name: str, *, owner: str | None = None,
                    private: bool = True) -> tuple[RepoRef, str]:
        """Create a repository. Returns the ref and its web URL."""

    @abstractmethod
    def repo_url(self, repo: RepoRef) -> str | None:
        """Web URL of an existing repo, or None if it is not there."""

    @abstractmethod
    def labels(self, repo: RepoRef) -> set[str]:
        """Existing label names. Read-only, so a plan can be costed before approval."""

    @abstractmethod
    def milestones(self, repo: RepoRef) -> dict[str, int]:
        """Existing milestones, title to provider-side number. Read-only."""

    @abstractmethod
    def ensure_label(self, repo: RepoRef, name: str, color: str,
                     description: str) -> bool:
        """Create the label if absent. Returns True if it was created.

        Never restyles a label that already exists — the user's own colours are
        not the harness's to overwrite.
        """

    @abstractmethod
    def ensure_milestone(self, repo: RepoRef, title: str,
                         description: str) -> tuple[int, bool]:
        """Create the milestone if absent. Returns ``(number, created)``."""

    @abstractmethod
    def create_issue(self, repo: RepoRef, spec: IssueSpec) -> Issue:
        ...

    @abstractmethod
    def get_issue(self, repo: RepoRef, number: int) -> Issue | None:
        ...

    @abstractmethod
    def update_issue(self, repo: RepoRef, number: int, spec: IssueSpec) -> Issue:
        ...

    @abstractmethod
    def open_pull_request(self, repo: RepoRef, *, head: str, base: str, title: str,
                          body: str, draft: bool = True) -> tuple[int, str]:
        """Open a change proposal. Returns ``(number, url)``.

        Draft by default: phase 3 ends at a human's judgement, and a
        ready-for-review PR asserts more than an approved ticket has earned.
        """
