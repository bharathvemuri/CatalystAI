"""Devcontainer execution — one container per active worktree (spec section 10).

Resolved decisions this implements: the runtime is the Devcontainer spec built
on Docker, orchestration is *host-based* (the harness runs natively and spins up
one container per ticket), and the base image is ``ubuntu:latest`` with no
further assumptions.

Host-based orchestration is the load-bearing choice. Nesting the whole pipeline
inside one outer container would force Docker-in-Docker or mounting the host
socket inward, both of which dissolve the isolation the section exists for.

The container is long-lived for the duration of a ticket rather than one per
command, because a bootstrap that installs the project's dependencies is
expensive and everything after it depends on those installs being present.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

from .execution import CommandResult, ExecutionError, Executor

IMAGE_TAG = "ai-harness/base:0.1"
CONTAINER_PREFIX = "ai-harness"
WORKSPACE = "/workspace"

NO_DOCKER = (
    "Docker is not available, and spec section 10 requires project-level work to "
    "run inside a container.\n"
    "  Install Docker Desktop:  https://docs.docker.com/get-started/get-docker/\n"
    "  or waive isolation:      --no-container\n"
    "Waiving is recorded in the event log, because a run that touched the host "
    "directly is not the same evidence as one that did not."
)


def docker_available() -> tuple[bool, str]:
    """Whether a usable Docker daemon is reachable. Returns (ok, detail)."""
    if not shutil.which("docker"):
        return False, "the `docker` command is not on PATH"
    try:
        probe = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run docker: {exc}"
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "").strip().splitlines()
        return False, detail[-1] if detail else "the Docker daemon is not responding"
    return True, f"Docker server {probe.stdout.strip()}"


def container_name(project_root: Path, ticket: str) -> str:
    """Stable per (project, ticket), so a resumed run finds its own container.

    The project root is hashed rather than embedded: two checkouts of the same
    repository must not collide, and a path is not a legal container name.
    """
    digest = hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()[:10]
    return f"{CONTAINER_PREFIX}-{digest}-{ticket.lower()}"


class DockerExecutor(Executor):
    """Runs commands inside one container bound to one worktree."""

    def __init__(self, root: Path, ticket: str, image: str = IMAGE_TAG,
                 env: dict[str, str] | None = None, extra_mounts: dict[Path, str] | None = None):
        super().__init__(root=root, env=env or {})
        self.ticket = ticket
        self.image = image
        self.name = container_name(root, ticket)
        self.extra_mounts = extra_mounts or {}
        self._started = False

    # ------------------------------------------------------------- lifecycle

    def _docker(self, argv: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["docker", *argv], capture_output=True, text=True,
                                  timeout=timeout, errors="replace")
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExecutionError(f"docker {' '.join(argv)}: {exc}") from exc

    def _exists(self) -> bool:
        probe = self._docker(["container", "inspect", self.name, "--format", "{{.State.Running}}"])
        return probe.returncode == 0

    def _running(self) -> bool:
        probe = self._docker(["container", "inspect", self.name, "--format", "{{.State.Running}}"])
        return probe.returncode == 0 and probe.stdout.strip() == "true"

    def ensure_image(self, dockerfile: Path) -> None:
        probe = self._docker(["image", "inspect", self.image])
        if probe.returncode == 0:
            return
        built = self._docker(
            ["build", "-t", self.image, "-f", str(dockerfile), str(dockerfile.parent)],
            timeout=1800)
        if built.returncode != 0:
            raise ExecutionError(
                f"could not build the harness base image:\n{built.stderr.strip()}")

    def start(self) -> None:
        if self._started and self._running():
            return
        if self._running():
            self._started = True
            return
        if self._exists():
            started = self._docker(["start", self.name])
            if started.returncode != 0:
                raise ExecutionError(f"could not start {self.name}: {started.stderr.strip()}")
            self._started = True
            return

        argv = ["run", "-d", "--name", self.name,
                "-v", f"{self.root}:{WORKSPACE}",
                "-w", WORKSPACE]
        for host_path, mount_point in self.extra_mounts.items():
            argv += ["-v", f"{host_path}:{mount_point}:ro"]
        for key, value in self.env.items():
            argv += ["-e", f"{key}={value}"]
        # No published ports and no host network: a ticket's container talks to
        # the outside only through what the agent explicitly runs.
        argv += [self.image, "sleep", "infinity"]

        created = self._docker(argv)
        if created.returncode != 0:
            raise ExecutionError(
                f"could not create the container for {self.ticket}:\n{created.stderr.strip()}")
        self._started = True

    def dispose(self) -> None:
        if self._exists():
            self._docker(["rm", "-f", self.name])
        self._started = False

    # --------------------------------------------------------------- running

    def run(self, argv, *, cwd=None, env=None, timeout=1800) -> CommandResult:
        self.start()
        inner_cwd = WORKSPACE
        if cwd is not None:
            try:
                relative = Path(cwd).resolve().relative_to(self.root)
                inner_cwd = f"{WORKSPACE}/{relative.as_posix()}".rstrip("/")
            except ValueError:
                inner_cwd = WORKSPACE

        exec_argv = ["exec", "-w", inner_cwd]
        for key, value in (env or {}).items():
            exec_argv += ["-e", f"{key}={value}"]
        exec_argv += [self.name, *argv]

        started = time.monotonic()
        completed = self._docker(exec_argv, timeout=timeout)
        return CommandResult(
            argv=list(argv), exit_code=completed.returncode,
            stdout=completed.stdout or "", stderr=completed.stderr or "",
            duration_ms=int((time.monotonic() - started) * 1000),
            where=f"container {self.name}",
        )

    def describe(self) -> str:
        return f"devcontainer {self.name} ({self.image})"


def bootstrap_commands(root: Path) -> list[list[str]]:
    """Install exactly what the project declares, fresh, every run (spec §10).

    Detection is by declaration file, never by guessing a stack — a repository
    that declares nothing gets nothing installed rather than a plausible guess.
    """
    commands: list[list[str]] = []
    if (root / "package-lock.json").is_file():
        commands.append(["npm", "ci"])
    elif (root / "package.json").is_file():
        commands.append(["npm", "install"])
    if (root / "requirements.txt").is_file():
        commands.append(["pip", "install", "-r", "requirements.txt"])
    if (root / "pyproject.toml").is_file():
        commands.append(["pip", "install", "-e", "."])
    if (root / "go.mod").is_file():
        commands.append(["go", "mod", "download"])
    if (root / "Cargo.toml").is_file():
        commands.append(["cargo", "fetch"])
    return commands


def devcontainer_json(image: str = IMAGE_TAG) -> str:
    """The Devcontainer descriptor written into a ticket worktree.

    Its purpose is compatibility, not orchestration: the harness drives Docker
    itself, but a human opening the worktree in VS Code or Codespaces gets the
    same environment the agents ran in.
    """
    return json.dumps({
        "name": "ai-harness ticket",
        "image": image,
        "workspaceFolder": WORKSPACE,
        "remoteUser": "root",
        "customizations": {"vscode": {"extensions": []}},
    }, indent=2) + "\n"
