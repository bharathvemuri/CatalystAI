"""Where a command actually runs.

Spec section 10 requires every project-level operation — installs, builds,
tests, agent tool calls — to happen inside a container rather than on the host,
for two reasons: "never assume a tool exists, always install it" is only safe
when installs are sandboxed, and autonomous agents running arbitrary build
commands should not be doing so against the developer's machine.

Everything above this module names an ``Executor`` and never learns whether the
command landed in a container or on the host. That is what makes the
``--no-container`` escape hatch a single, auditable substitution rather than a
second code path threaded through the pipeline — and why waiving isolation is
recorded as an event, not just a flag.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT = 1800


class ExecutionError(RuntimeError):
    """The command could not be run at all — distinct from running and failing."""


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    where: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class Executor(ABC):
    root: Path
    env: dict[str, str] = field(default_factory=dict)

    @abstractmethod
    def run(self, argv: list[str], *, cwd: Path | None = None,
            env: dict[str, str] | None = None,
            timeout: int = DEFAULT_TIMEOUT) -> CommandResult:
        ...

    @abstractmethod
    def describe(self) -> str:
        """One line for the plan and the event log."""

    @property
    def isolated(self) -> bool:
        return True

    def dispose(self) -> None:
        """Release anything long-lived. Safe to call more than once."""


class HostExecutor(Executor):
    """Runs on the machine the harness is on. Not isolated, and says so."""

    def run(self, argv, *, cwd=None, env=None, timeout=DEFAULT_TIMEOUT) -> CommandResult:
        import time

        merged = {**os.environ, **self.env, **(env or {})}
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv, cwd=str(cwd or self.root), env=merged, timeout=timeout,
                capture_output=True, text=True, errors="replace",
            )
        except FileNotFoundError as exc:
            raise ExecutionError(f"{argv[0]!r} is not available on this machine") from exc
        except subprocess.TimeoutExpired as exc:
            raise ExecutionError(f"{' '.join(argv)} timed out after {timeout}s") from exc

        return CommandResult(
            argv=list(argv), exit_code=completed.returncode,
            stdout=completed.stdout or "", stderr=completed.stderr or "",
            duration_ms=int((time.monotonic() - started) * 1000), where="host",
        )

    def describe(self) -> str:
        return f"host ({self.root})"

    @property
    def isolated(self) -> bool:
        return False


def posix_shell() -> str | None:
    """A POSIX shell for running gate scripts on the host.

    Gate scripts are shell by design (spec section 5.3 wants them to be scripts,
    not prompts) and normally run inside the Linux devcontainer. On a Windows
    host without containers, Git's bundled sh is the only reason host mode works
    at all, so it is looked for explicitly rather than assumed absent.
    """
    for candidate in ("sh", "bash"):
        found = shutil.which(candidate)
        if found:
            return found
    for guess in (r"C:\Program Files\Git\bin\bash.exe",
                  r"C:\Program Files\Git\usr\bin\sh.exe"):
        if Path(guess).is_file():
            return guess
    return None
