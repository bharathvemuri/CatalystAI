"""Deterministic gates (spec section 5.3).

Every blocking condition in an agent contract — build fails, lint fails, tests
fail, CRITICAL/HIGH security findings — is decided by a script that actually
ran, never by asking a model whether it passed. This module runs those scripts
through an ``Executor`` and turns their output into validated results the
Reviewer agent *reads* rather than re-derives.

Two rules keep the gate honest. A gate that skipped is not a gate that passed:
whether a skip is tolerated is a threshold decision, made here, not inside the
script. And a result that does not satisfy ``gate-result.schema.json`` is a gate
failure, because a gate whose own output is malformed has not demonstrated
anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import contracts
from .contracts import ContractViolation
from .execution import CommandResult, ExecutionError, Executor
from .paths import Project
from .thresholds import Thresholds

GATE_NAMES = ("build", "test", "lint", "security-scan")
GATES_MOUNT = "/harness/gates"


@dataclass
class GateResult:
    gate: str
    status: str
    exit_code: int
    summary: str
    command: str = ""
    evidence_file: str = ""
    evidence_tail: str = ""
    duration_ms: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    @property
    def skipped(self) -> bool:
        return self.status == "skip"

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate, "status": self.status, "exit_code": self.exit_code,
            "summary": self.summary, "command": self.command,
            "evidence_file": self.evidence_file, "evidence_tail": self.evidence_tail,
            "duration_ms": self.duration_ms, "metrics": self.metrics,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GateResult":
        return cls(
            gate=data["gate"], status=data["status"], exit_code=data.get("exit_code", 0),
            summary=data.get("summary", ""), command=data.get("command", ""),
            evidence_file=data.get("evidence_file", ""),
            evidence_tail=data.get("evidence_tail", ""),
            duration_ms=data.get("duration_ms", 0), metrics=data.get("metrics") or {},
        )


def _extract_result(stdout: str) -> dict[str, Any] | None:
    """The last line that parses as a JSON object.

    Gate scripts route noise to the evidence file, but a build tool that writes
    to stdout anyway must not be able to corrupt the machine-readable result.
    """
    for line in reversed([ln.strip() for ln in stdout.splitlines() if ln.strip()]):
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "gate" in parsed:
            return parsed
    return None


def report_dir_for(project: Project, ticket: str) -> Path:
    return project.reports / ticket


def relative_report_dir(ticket: str) -> str:
    """Report path as the gate script sees it — relative, so it means the same
    thing on the host and inside the container."""
    return f".harness/reports/{ticket}"


def run_gate(name: str, executor: Executor, *, ticket: str, gates_dir: str,
             shell: str = "sh", project: Project | None = None,
             timeout: int = 1800) -> GateResult:
    """Run one gate script and return its validated result."""
    env = {
        "HARNESS_REPORT_DIR": relative_report_dir(ticket),
        "HARNESS_GATE": name,
        "HARNESS_TICKET": ticket,
    }
    script = f"{gates_dir.rstrip('/')}/{name}.sh"

    try:
        outcome: CommandResult = executor.run([shell, script], env=env, timeout=timeout)
    except ExecutionError as exc:
        return GateResult(gate=name, status="fail", exit_code=-1,
                          summary=f"the gate could not be run: {exc}")

    payload = _extract_result(outcome.stdout)
    if payload is None:
        tail = (outcome.stderr or outcome.stdout or "").strip()[-2000:]
        return GateResult(
            gate=name, status="fail", exit_code=outcome.exit_code,
            summary="the gate produced no parseable result",
            evidence_tail=tail, duration_ms=outcome.duration_ms)

    payload.setdefault("exit_code", outcome.exit_code)
    payload.setdefault("duration_ms", outcome.duration_ms)

    try:
        contracts.validate("gate-result", payload, project)
    except ContractViolation as exc:
        return GateResult(
            gate=name, status="fail", exit_code=outcome.exit_code,
            summary=f"the gate result does not satisfy its contract: {exc}",
            duration_ms=outcome.duration_ms)

    return GateResult.from_dict(payload)


def run_all(executor: Executor, *, ticket: str, gates_dir: str, project: Project,
            names: tuple[str, ...] = GATE_NAMES, shell: str = "sh") -> list[GateResult]:
    return [run_gate(name, executor, ticket=ticket, gates_dir=gates_dir,
                     shell=shell, project=project)
            for name in names]


def verdict(results: list[GateResult], thresholds: Thresholds) -> tuple[bool, list[str]]:
    """Whether the gates permit the ticket to advance, and why not if they do not."""
    reasons: list[str] = []
    seen = {r.gate for r in results}

    for result in results:
        if result.status == "fail":
            reasons.append(f"{result.gate}: {result.summary}")
        elif result.skipped and thresholds.gate_is_required(result.gate):
            reasons.append(
                f"{result.gate}: required by thresholds.yaml but skipped "
                f"({result.summary})")

    for required in thresholds.required_gates:
        if required not in seen:
            reasons.append(f"{required}: required but never ran")

    return not reasons, reasons


def write_results(project: Project, ticket: str, results: list[GateResult]) -> Path:
    """Persist the structured results the Reviewer reads instead of re-deriving."""
    directory = report_dir_for(project, ticket)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "gate-results.json"
    path.write_text(
        json.dumps([r.to_dict() for r in results], indent=2) + "\n", encoding="utf-8")
    return path


def read_results(project: Project, ticket: str) -> list[GateResult]:
    path = report_dir_for(project, ticket) / "gate-results.json"
    if not path.is_file():
        return []
    return [GateResult.from_dict(item)
            for item in json.loads(path.read_text(encoding="utf-8"))]


def render_summary(results: list[GateResult]) -> str:
    """A compact table for the human approval digest and the CLI."""
    if not results:
        return "no gates ran"
    width = max(len(r.gate) for r in results)
    lines = []
    for result in results:
        mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[result.status]
        lines.append(f"  {result.gate.ljust(width)}  {mark}  {result.summary}")
    return "\n".join(lines)
