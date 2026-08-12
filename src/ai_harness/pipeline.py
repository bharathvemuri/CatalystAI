"""Per-ticket orchestration (spec section 5.2).

Strictly linear within a ticket — Architect, Developer, gates, the reviewing
fan-out, Reviewer — and parallel across tickets, gated only by ``depends_on``.
This module owns the within-ticket half; ``commands/run.py`` owns the across.

Two things are structural rather than incidental. Every step appends to the
ticket's *own* event stream, so a ticket that crashes mid-pipeline is resumable
from its log and two tickets running at once never write the same file. And the
Reviewer's verdict is checked against the gates before it is believed: an
APPROVE issued while the build is failing is rejected and recorded as a contract
violation, because section 5.3 puts the deterministic gate above the model's
opinion, not beside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import agents, gates, worktrees
from .agents import AgentError, AgentResult
from .context_index import ContextIndex, ContextIndexUnavailable
from .events import EventLog
from .execution import Executor
from .gates import GateResult
from .paths import Project
from .registry import Registry
from .taskfile import TaskFile
from .thresholds import Thresholds

REVIEW_FAN_OUT = ("security", "qa", "performance")
POST_APPROVAL = ("documentation", "devops")


class PipelineError(RuntimeError):
    """The ticket cannot proceed. Distinct from a ticket the Reviewer blocked."""


@dataclass
class TicketOutcome:
    ticket: str
    status: str                       # awaiting_approval | blocked | failed
    decision: str | None = None
    cycles: int = 0
    detail: str = ""
    gate_results: list[GateResult] = field(default_factory=list)
    reports: dict[str, Path] = field(default_factory=dict)


def _render(text: str) -> str:
    return text.strip() or "(empty)"


def _prompt_for(agent: str, task: TaskFile, worktree, extra: dict[str, str]) -> str:
    """Assemble the agent's inputs. The contract says what to do; this says with what."""
    parts = [
        "<ticket>",
        task.path.read_text(encoding="utf-8"),
        "</ticket>",
        "",
        f"You are working in a git worktree at the repository root, on branch "
        f"{worktree.branch}. Paths in tool calls are relative to that root.",
        "",
    ]
    for name, body in extra.items():
        parts += [f"<{name}>", _render(body), f"</{name}>", ""]
    parts.append(f"Carry out your contract for this ticket as the {agent} agent, "
                 "then return your result.")
    return "\n".join(parts)


class TicketPipeline:
    """Drives one ticket from Architect to the human approval gate."""

    def __init__(self, project: Project, task: TaskFile, *, log: EventLog,
                 executor: Executor, worktree: worktrees.Worktree,
                 registry: Registry, thresholds: Thresholds,
                 gates_dir: str, shell: str = "sh",
                 announce: Callable[[str], None] = lambda _m: None):
        self.project = project
        self.task = task
        self.ticket = task.id
        self.log = log
        self.executor = executor
        self.worktree = worktree
        self.registry = registry
        self.thresholds = thresholds
        self.gates_dir = gates_dir
        self.shell = shell
        self.announce = announce
        self.index: ContextIndex | None = None
        self.models: dict[str, str] = {}
        self.reports: dict[str, Path] = {}

    # ------------------------------------------------------------- plumbing

    def _emit(self, type: str, payload: dict[str, Any]) -> None:
        self.log.append(type, payload, stream=self.ticket)

    def _status(self, status: str) -> None:
        self._emit("task.status_changed", {"task_id": self.ticket, "status": status})

    def _run_agent(self, agent: str, extra: dict[str, str]) -> AgentResult:
        self.announce(f"  {self.ticket}  {agent} ...")
        self._emit("task.agent_started", {"task_id": self.ticket, "agent": agent})
        prompt = _prompt_for(agent, self.task, self.worktree, extra)
        try:
            result = agents.run(
                agent, project=self.project, root=self.worktree.path,
                executor=self.executor, user_prompt=prompt, registry=self.registry,
                model=self.models.get(agent), index=self.index, shell=self.shell,
            )
        except AgentError as exc:
            raise PipelineError(str(exc)) from exc

        self.reports[agent] = agents.write_report(self.project, self.ticket, agent, result)
        self._emit("task.agent_completed", {
            "task_id": self.ticket, "agent": agent, "model": result.model,
            "result": "blocked" if result.blocked else "complete",
            "summary": result.result.get("summary", ""),
            "tool_calls": len(result.transcript),
            "violations": result.violations,
        })
        if result.violations:
            self.announce(f"    contract violation(s): {'; '.join(result.violations)}")
        if result.blocked:
            raise PipelineError(f"{agent} blocked: {result.blocking_reason}")
        return result

    def build_index(self) -> None:
        try:
            index = ContextIndex(self.worktree.path)
            stats = index.build()
            index.save(self.project)
            self.index = index
            self.announce(f"  {self.ticket}  index: {stats['indexed']} file(s) parsed, "
                          f"{stats['reused']} reused")
        except ContextIndexUnavailable as exc:
            # Spec section 9 wants the index; running without it is a real
            # degradation, so it is announced and recorded rather than assumed.
            self.announce(f"  {self.ticket}  context index unavailable: {exc}")
            self._emit("ticket.index_unavailable", {"task_id": self.ticket, "reason": str(exc)})

    # ---------------------------------------------------------------- gates

    def run_gates(self) -> tuple[bool, list[GateResult], list[str]]:
        self.announce(f"  {self.ticket}  gates ...")
        results = gates.run_all(self.executor, ticket=self.ticket,
                                gates_dir=self.gates_dir, project=self.project,
                                shell=self.shell)
        gates.write_results(self.project, self.ticket, results)
        passed, reasons = gates.verdict(results, self.thresholds)
        self._emit("ticket.gates_ran", {
            "task_id": self.ticket, "passed": passed,
            "results": [{"gate": r.gate, "status": r.status, "summary": r.summary}
                        for r in results],
            "reasons": reasons,
        })
        self.announce(gates.render_summary(results))
        return passed, results, reasons

    # ------------------------------------------------------------- the run

    def run(self) -> TicketOutcome:
        self._status("in_progress")
        self.build_index()

        architect = self._run_agent("architect", {})
        overrides = architect.result.get("model_overrides") or {}
        for agent, model in overrides.items():
            if agent in agents.AGENTS:
                self.models[agent] = self.registry.validate(model)
        assigned = {a: self.models.get(a, self.registry.default_for(a))
                    for a in agents.AGENTS if a != "architect"}
        self._emit("task.models_assigned", {"task_id": self.ticket,
                                            "assigned_models": assigned})

        plan = architect.result.get("plan_markdown", "")
        if architect.result.get("adr_required") and architect.result.get("adr_markdown"):
            self._write_adr(architect.result["adr_markdown"])

        gate_results: list[GateResult] = []
        feedback = ""
        max_cycles = self.thresholds.max_block_cycles

        for cycle in range(1, max_cycles + 1):
            developer = self._run_agent("developer", {
                "implementation_plan": plan,
                **({"reviewer_feedback": feedback} if feedback else {}),
            })
            sha = worktrees.commit_all(
                self.worktree,
                f"{self.ticket}: {self.task.frontmatter['title']} (cycle {cycle})")
            if sha:
                self._emit("ticket.committed", {"task_id": self.ticket,
                                                "sha": sha, "cycle": cycle})

            passed, gate_results, reasons = self.run_gates()
            diff = worktrees.diff(self.worktree, self._base_ref())

            evidence = {
                "implementation_plan": plan,
                "diff": diff,
                "gate_results": gates.render_summary(gate_results),
                "developer_report": developer.result.get("report_markdown", ""),
            }
            reviews = {}
            for agent in REVIEW_FAN_OUT:
                reviews[agent] = self._run_agent(agent, evidence)

            reviewer = self._run_agent("reviewer", {
                **evidence,
                "security_report": reviews["security"].result.get("report_markdown", ""),
                "qa_report": reviews["qa"].result.get("report_markdown", ""),
                "performance_report": reviews["performance"].result.get("report_markdown", ""),
                "gate_verdict": "PASSED" if passed else "FAILED:\n" + "\n".join(reasons),
            })
            decision = reviewer.result.get("decision", "BLOCK")

            blocking = self._blocking_conditions(passed, reasons, reviews)
            if decision == "APPROVE" and blocking:
                # Section 5.3: the gate outranks the opinion.
                self.announce(f"  {self.ticket}  APPROVE rejected — {'; '.join(blocking)}")
                self._emit("task.review_decided", {
                    "task_id": self.ticket, "decision": "BLOCK", "cycle": cycle,
                    "model_decision": "APPROVE", "overridden": True,
                    "blocking_conditions": blocking,
                })
                decision = "BLOCK"
            else:
                self._emit("task.review_decided", {
                    "task_id": self.ticket, "decision": decision, "cycle": cycle,
                    "required_changes": reviewer.result.get("required_changes", []),
                })

            if decision == "APPROVE":
                self._status("review_approved")
                self._emit("task.human_approval_requested", {"task_id": self.ticket})
                return TicketOutcome(self.ticket, "awaiting_approval", decision="APPROVE",
                                     cycles=cycle, gate_results=gate_results,
                                     reports=dict(self.reports))

            feedback = "\n".join([
                f"Decision: {decision}",
                *(f"- {c}" for c in reviewer.result.get("required_changes", [])),
                *(f"- gate: {r}" for r in reasons),
            ])

        self._status("blocked")
        return TicketOutcome(self.ticket, "blocked", decision=decision, cycles=max_cycles,
                             detail=f"still not approved after {max_cycles} cycle(s)",
                             gate_results=gate_results, reports=dict(self.reports))

    # --------------------------------------------------------------- pieces

    def _base_ref(self) -> str:
        return self.task.frontmatter.get("_base_ref") or "HEAD"

    def _blocking_conditions(self, gates_passed: bool, reasons: list[str],
                             reviews: dict[str, AgentResult]) -> list[str]:
        """The Reviewer contract's cannot-approve list, checked mechanically."""
        blocking: list[str] = []
        if not gates_passed:
            blocking.extend(f"gate: {r}" for r in reasons)

        for agent in ("security", "qa", "performance"):
            result = reviews.get(agent)
            if result is None:
                continue
            for finding in result.result.get("findings", []) or []:
                if finding.get("severity") in ("CRITICAL", "HIGH") and agent == "security":
                    blocking.append(
                        f"security {finding['severity']}: {finding.get('title', '?')}")

        qa = reviews.get("qa")
        if qa is not None and (qa.result.get("tests_failed") or 0) > 0:
            blocking.append(f"qa: {qa.result['tests_failed']} test(s) failing")
        return blocking

    def _write_adr(self, body: str) -> None:
        """ADR numbers are allocated here, not by the agent — two tickets running
        in parallel would otherwise both claim the next number."""
        self.project.architecture.mkdir(parents=True, exist_ok=True)
        existing = sorted(self.project.architecture.glob("ADR-*.md"))
        numbers = []
        for path in existing:
            stem = path.stem.split("-")
            if len(stem) >= 2 and stem[1].isdigit():
                numbers.append(int(stem[1]))
        number = (max(numbers) + 1) if numbers else 1
        path = self.project.architecture / f"ADR-{number:03d}.md"
        path.write_text(body.rstrip() + "\n", encoding="utf-8")
        self._emit("ticket.adr_written", {"task_id": self.ticket,
                                          "path": str(path.name), "number": number})
        self.announce(f"  {self.ticket}  wrote {path.name}")


def ready_tickets(state: dict[str, Any], tasks: list[TaskFile]) -> list[TaskFile]:
    """Tickets whose dependencies are all done — the parallelism gate (spec 5.2)."""
    status = {tid: entry.get("status") for tid, entry in state.get("tasks", {}).items()}
    ready = []
    for task in tasks:
        if status.get(task.id) not in (None, "pending"):
            continue
        if all(status.get(dep) in ("done", "human_approved") for dep in task.depends_on):
            ready.append(task)
    return ready
