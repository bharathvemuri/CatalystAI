"""Isolated git worktrees, one per active ticket (spec section 11).

The alternative considered and rejected was running concurrent tickets in one
shared tree: it risks file, build and test collisions, and it does not survive a
crashed session cleanly. A worktree gives real filesystem isolation, composes
with one container per worktree for process isolation, and matches the
resumability model — a worktree that still exists after a crash is exactly where
the ticket was.

Worktrees live under ``.harness/worktrees/<ticket>`` rather than beside the
repository, so everything belonging to a project stays inside it and nothing is
orphaned somewhere a user will not think to look. Git allows this as long as the
directory is ignored, which ``harness init`` records in its gitignore hints.

Git runs on the *host*, never inside a ticket's container: orchestration is
host-based (resolved question #7), and a container that could rewrite the
repository's history would undo the isolation it exists to provide.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .paths import Project

BRANCH_PREFIX = "harness"


class GitError(RuntimeError):
    """A git command failed. Carries the stderr, because git's messages are good."""


@dataclass(frozen=True)
class Worktree:
    ticket: str
    path: Path
    branch: str


def branch_for(ticket: str) -> str:
    return f"{BRANCH_PREFIX}/{ticket}"


def worktrees_root(project: Project) -> Path:
    return project.harness / "worktrees"


def path_for(project: Project, ticket: str) -> Path:
    return worktrees_root(project) / ticket


def git(root: Path, *args: str, check: bool = True, timeout: int = 300) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=timeout, errors="replace")
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    except subprocess.SubprocessError as exc:
        raise GitError(f"git {' '.join(args)}: {exc}") from exc

    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise GitError(f"git {' '.join(args)} failed:\n{detail}")
    return completed.stdout


def is_repo(root: Path) -> bool:
    try:
        return git(root, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except GitError:
        return False


def head_ref(root: Path) -> str:
    return git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()


def branch_exists(root: Path, branch: str) -> bool:
    out = git(root, "branch", "--list", branch, check=False)
    return bool(out.strip())


def existing(project: Project) -> list[Worktree]:
    """Worktrees git currently knows about that belong to the harness."""
    listing = git(project.root, "worktree", "list", "--porcelain", check=False)
    found: list[Worktree] = []
    path: Path | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):].strip())
        elif line.startswith("branch ") and path is not None:
            ref = line[len("branch "):].strip()
            branch = ref.replace("refs/heads/", "")
            if branch.startswith(f"{BRANCH_PREFIX}/"):
                found.append(Worktree(ticket=branch.split("/", 1)[1], path=path, branch=branch))
            path = None
    return found


def ensure(project: Project, ticket: str, base: str | None = None) -> Worktree:
    """Create the ticket's worktree if it is not already there.

    Idempotent on purpose: a resumed run must reattach to the tree the previous
    run left rather than start again from the base revision and lose its work.
    """
    if not is_repo(project.root):
        raise GitError(
            f"{project.root} is not a git repository.\n"
            "Phase 3 runs each ticket in its own git worktree; initialise the "
            "repository first.")

    target = path_for(project, ticket)
    branch = branch_for(ticket)

    for worktree in existing(project):
        if worktree.ticket == ticket:
            if worktree.path.exists():
                return worktree
            # Registered but gone from disk — a crash or a manual delete.
            git(project.root, "worktree", "prune")
            break

    target.parent.mkdir(parents=True, exist_ok=True)
    base_ref = base or head_ref(project.root)

    if branch_exists(project.root, branch):
        git(project.root, "worktree", "add", str(target), branch)
    else:
        git(project.root, "worktree", "add", "-b", branch, str(target), base_ref)

    return Worktree(ticket=ticket, path=target, branch=branch)


def is_merged(root: Path, branch: str, base: str) -> bool:
    """Is every commit on ``branch`` already reachable from ``base``?

    Phase 4 documents what shipped, but a ticket's work lives on its own branch
    until the pull request is merged. A docs pass run against a base that does
    not contain a ticket's commits cannot see that ticket's code, and would
    otherwise document it from the reports alone as though it were there.
    """
    if not branch_exists(root, branch):
        return False
    try:
        git(root, "merge-base", "--is-ancestor", branch, base)
    except GitError:
        return False
    return True


def is_dirty(worktree: Worktree) -> bool:
    return bool(git(worktree.path, "status", "--porcelain", check=False).strip())


def changed_files(worktree: Worktree, base: str) -> list[str]:
    out = git(worktree.path, "diff", "--name-only", f"{base}...HEAD", check=False)
    tracked = [line.strip() for line in out.splitlines() if line.strip()]
    pending = git(worktree.path, "status", "--porcelain", check=False)
    for line in pending.splitlines():
        name = line[3:].strip()
        if name and name not in tracked:
            tracked.append(name)
    return tracked


def diff(worktree: Worktree, base: str, max_chars: int = 200_000) -> str:
    """The ticket's diff against its base, for the reviewing agents.

    Truncated rather than unbounded: a diff that does not fit is a signal the
    ticket was chunked too coarsely, and silently sending 2MB to a model is not
    a better outcome than saying so.
    """
    out = git(worktree.path, "diff", f"{base}...HEAD", check=False)
    staged = git(worktree.path, "diff", "HEAD", check=False)
    combined = out + staged
    if len(combined) > max_chars:
        return combined[:max_chars] + (
            f"\n\n[diff truncated at {max_chars} characters; "
            "the ticket may be too large to review as one unit]")
    return combined


def history(root: Path, *, max_commits: int = 40, max_chars: int = 20_000) -> str:
    """Recent history, rendered for an agent that cannot run git itself.

    Git is host-only by design (see the module docstring), but the Architect's
    contract needs to know how an area has changed before. Reading it out here
    and passing it as context satisfies that without mounting the repository's
    ``.git`` into a ticket's container, which would hand a container the ability
    to rewrite the history the isolation exists to protect.

    Scoped to the branch rather than to the ticket's paths on purpose: a task's
    ``dir`` is a tracker label, not a guaranteed directory, and inferring one
    from the other would be the kind of guess this harness refuses elsewhere.
    """
    out = git(root, "log", f"-n{max_commits}", "--no-merges", "--date=short",
              "--pretty=format:%h %ad %an: %s", check=False).strip()
    if not out:
        return ("This repository has no commit history yet — this is the first "
                "work on it. Do not infer prior conventions from its absence.")
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n[history truncated at {max_chars} characters]"
    return out


def commit_all(worktree: Worktree, message: str) -> str | None:
    """Commit everything in the worktree. Returns the sha, or None if clean."""
    if not is_dirty(worktree):
        return None
    git(worktree.path, "add", "-A")
    git(worktree.path, "commit", "-m", message)
    return git(worktree.path, "rev-parse", "HEAD").strip()


def push(worktree: Worktree, remote: str = "origin") -> None:
    git(worktree.path, "push", "-u", remote, worktree.branch)


def remove(project: Project, ticket: str, *, force: bool = False) -> bool:
    """Drop a ticket's worktree. Refuses to discard uncommitted work silently."""
    for worktree in existing(project):
        if worktree.ticket != ticket:
            continue
        if is_dirty(worktree) and not force:
            raise GitError(
                f"{worktree.path} has uncommitted changes; pass force to discard them")
        args = ["worktree", "remove", str(worktree.path)]
        if force:
            args.append("--force")
        git(project.root, *args)
        return True
    return False
