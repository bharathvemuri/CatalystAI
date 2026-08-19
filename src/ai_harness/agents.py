"""Running one agent against one ticket.

Spec section 5 gives every agent a contract — Mission, Inputs, Responsibilities,
Non-Responsibilities, Required Tools, Required Output, Blocking Conditions,
Success, Failure. Those contracts are portable markdown in ``core/agents/``, so
porting the harness to another runner means writing a new wrapper rather than
rewriting the agents.

The part that cannot be portable markdown is enforcement. A contract saying
"do not modify application source code" is a sentence until something rejects
the write, so the boundary table from the framework document is implemented
here as a real check on every write tool call, and a violation is recorded as
a contract violation rather than quietly allowed. Commands run through the
ticket's ``Executor``, which is what keeps an agent's shell inside its own
container instead of on the developer's machine.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import runner as runners
from .context_index import ContextIndex
from .execution import ExecutionError, Executor
from .llm import LLMError
from .paths import Project
from .registry import Registry

AGENTS = ("architect", "developer", "security", "qa", "performance",
          "reviewer", "documentation", "devops")

# The areas the Reviewer renders a per-area verdict for (reviewer.md). Kept here
# so the result schema can enumerate them: the API's structured-output mode
# rejects an open-ended string map, so verdicts is a list keyed by these.
REVIEW_AREAS = ("requirements", "architecture", "correctness", "security",
                "testing", "performance", "maintainability", "documentation")

# A *role* is a contract an agent can be run under. Usually one per agent, but
# the Documentation agent has two missions with the same write scope and the
# same model: the per-ticket pass, and the phase 4 project pass. Keeping that as
# a second contract rather than a second agent means the registry, the boundary
# table and the report filename all stay keyed by the agent they belong to.
ROLES = AGENTS + ("documentation-project",)

ROLE_AGENT = {"documentation-project": "documentation"}

PREAMBLE = "agents/_preamble.md"

# The boundary table from the framework document, as enforcement rather than
# prose. None means the agent may not write at all.
WRITE_SCOPES: dict[str, str | None] = {
    "architect": None,
    "developer": "any",
    "security": None,
    "qa": "tests",
    "performance": "benchmarks",
    "reviewer": None,
    "documentation": "docs",
    "devops": "infra",
}

SCOPE_PATTERNS: dict[str, tuple[str, ...]] = {
    "tests": ("test_*.py", "*_test.py", "*_test.go", "*.test.*", "*.spec.*",
              "tests/*", "test/*", "spec/*", "__tests__/*"),
    "benchmarks": ("bench_*.py", "*_bench.py", "*_bench.go", "*.bench.*",
                   "bench/*", "benchmarks/*"),
    "docs": ("*.md", "*.mdx", "*.rst", "*.txt", "docs/*", "doc/*"),
    "infra": ("Dockerfile*", "docker-compose*.yml", "docker-compose*.yaml",
              "Makefile", "*.tf", "*.tfvars", ".github/*", ".gitlab-ci.yml",
              "k8s/*", "deploy/*", "infra/*", "charts/*", "*.service"),
}

MAX_READ_BYTES = 200_000


class AgentError(RuntimeError):
    """The agent could not be run, or broke its contract."""


@dataclass
class AgentResult:
    agent: str
    model: str
    effort: str
    result: dict[str, Any]
    backend: str = runners.API
    transcript: list[dict[str, Any]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.result.get("status") == "blocked"

    @property
    def blocking_reason(self) -> str:
        return self.result.get("blocking_reason") or ""


# ------------------------------------------------------------------ contracts

def load_contract(project: Project, role: str) -> str:
    """Preamble plus the role's own contract, as one system prompt."""
    if role not in ROLES:
        raise AgentError(f"unknown role {role!r}; known: {', '.join(ROLES)}")
    try:
        preamble = project.resolve(PREAMBLE).read_text(encoding="utf-8")
        contract = project.resolve(f"agents/{role}.md").read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AgentError(f"{role} has no contract: {exc}") from exc
    return f"{preamble}\n\n---\n\n{contract}"


def may_write(agent: str, relpath: str) -> bool:
    scope = WRITE_SCOPES.get(agent)
    if scope is None:
        return False
    if scope == "any":
        return True
    candidate = relpath.replace("\\", "/")
    for pattern in SCOPE_PATTERNS[scope]:
        if fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(candidate, f"*/{pattern}"):
            return True
        if pattern.endswith("/*") and candidate.startswith(pattern[:-2] + "/"):
            return True
    return False


# --------------------------------------------------------------------- tools

TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read a file from the ticket's worktree. Use this when you "
                       "are about to change a file or must reason about its exact "
                       "contents; use index_query first to find out where something "
                       "is, rather than reading files to search.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the repository root."},
                "start_line": {"type": "integer", "description": "1-based first line to return."},
                "line_count": {"type": "integer", "description": "How many lines to return."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": "List the entries of a directory in the worktree.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "index_query",
        "description": "Query the context index instead of reading whole files. "
                       "'definitions' finds where a symbol is defined, 'callers' finds "
                       "files that call it, 'importers' finds files importing a module, "
                       "'outline' lists the symbols in one file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "enum": ["definitions", "callers", "importers", "outline"]},
                "name": {"type": "string",
                         "description": "Symbol name, module fragment, or file path for 'outline'."},
            },
            "required": ["kind", "name"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or replace a file. Rejected if your contract does not "
                       "permit writing that path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace one exact occurrence of old_text with new_text. "
                       "Fails if old_text appears zero times or more than once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the ticket's environment and return its "
                       "combined output and exit code. This is how you obtain evidence: "
                       "a build, a test run, a scanner. Never report a result you did "
                       "not run.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]

READ_ONLY_TOOLS = {"read_file", "list_dir", "index_query", "run_command"}


class ToolBox:
    """Executes tool calls for one agent, inside one worktree."""

    def __init__(self, agent: str, root: Path, executor: Executor,
                 index: ContextIndex | None = None, shell: str = "sh"):
        self.agent = agent
        self.root = Path(root).resolve()
        self.executor = executor
        self.index = index
        self.shell = shell
        self.violations: list[str] = []
        self.written: set[str] = set()

    def _resolve(self, path: str) -> Path:
        candidate = (self.root / path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise AgentError(f"{path!r} is outside the ticket worktree")
        return candidate

    def dispatch(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return f"unknown tool {name!r}", True
        try:
            return handler(args), False
        except AgentError as exc:
            return str(exc), True
        except (OSError, ValueError) as exc:
            return f"{type(exc).__name__}: {exc}", True

    # ----------------------------------------------------------- read tools

    def _t_read_file(self, args: dict[str, Any]) -> str:
        path = self._resolve(args["path"])
        if not path.is_file():
            raise AgentError(f"{args['path']} is not a file")
        if path.stat().st_size > MAX_READ_BYTES:
            raise AgentError(f"{args['path']} is larger than {MAX_READ_BYTES} bytes; "
                             "read a line range or query the index instead")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(args.get("start_line") or 1))
        count = int(args.get("line_count") or len(lines))
        selected = lines[start - 1:start - 1 + count]
        return "\n".join(f"{start + i:>6}  {line}" for i, line in enumerate(selected))

    def _t_list_dir(self, args: dict[str, Any]) -> str:
        path = self._resolve(args["path"])
        if not path.is_dir():
            raise AgentError(f"{args['path']} is not a directory")
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
        return "\n".join(f"{'d' if e.is_dir() else 'f'}  {e.name}" for e in entries) or "(empty)"

    def _t_index_query(self, args: dict[str, Any]) -> str:
        if self.index is None:
            raise AgentError("the context index is not available for this run")
        kind, name = args["kind"], args["name"]
        if kind == "definitions":
            found = self.index.definitions(name)
            return "\n".join(f"{p}:{s.line}  {s.kind} {s.name}" for p, s in found) or "(none)"
        if kind == "callers":
            return "\n".join(self.index.callers(name)) or "(none)"
        if kind == "importers":
            return "\n".join(self.index.importers(name)) or "(none)"
        if kind == "outline":
            symbols = self.index.outline(name)
            return "\n".join(f"{s.line:>6}  {s.kind} {s.name}" for s in symbols) or "(none)"
        raise AgentError(f"unknown index query {kind!r}")

    # ---------------------------------------------------------- write tools

    def _check_write(self, relpath: str) -> None:
        if may_write(self.agent, relpath):
            return
        scope = WRITE_SCOPES.get(self.agent)
        detail = ("your contract does not permit writing any file"
                  if scope is None else f"your contract permits writing {scope} files only")
        self.violations.append(f"{self.agent} attempted to write {relpath}")
        raise AgentError(f"refused: {detail}, and {relpath} is not one of them")

    def _t_write_file(self, args: dict[str, Any]) -> str:
        relpath = args["path"].replace("\\", "/")
        self._check_write(relpath)
        path = self._resolve(relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        self.written.add(relpath)
        return f"wrote {relpath} ({len(args['content'])} bytes)"

    def _t_edit_file(self, args: dict[str, Any]) -> str:
        relpath = args["path"].replace("\\", "/")
        self._check_write(relpath)
        path = self._resolve(relpath)
        if not path.is_file():
            raise AgentError(f"{relpath} does not exist")
        text = path.read_text(encoding="utf-8")
        occurrences = text.count(args["old_text"])
        if occurrences == 0:
            raise AgentError(f"old_text does not appear in {relpath}")
        if occurrences > 1:
            raise AgentError(f"old_text appears {occurrences} times in {relpath}; "
                             "include more surrounding context to make it unique")
        path.write_text(text.replace(args["old_text"], args["new_text"], 1), encoding="utf-8")
        self.written.add(relpath)
        return f"edited {relpath}"

    # -------------------------------------------------------------- command

    def _t_run_command(self, args: dict[str, Any]) -> str:
        command = args["command"]
        try:
            outcome = self.executor.run([self.shell, "-c", command])
        except ExecutionError as exc:
            raise AgentError(str(exc)) from exc
        body = (outcome.stdout or "") + (outcome.stderr or "")
        if len(body) > 20000:
            body = body[:20000] + "\n[output truncated]"
        return f"exit {outcome.exit_code} ({outcome.where})\n{body}"


# ------------------------------------------------------------------ schemas

def _base_properties() -> dict[str, Any]:
    return {
        "status": {"type": "string", "enum": ["complete", "blocked"]},
        "blocking_reason": {"type": "string",
                            "description": "Empty unless status is 'blocked'."},
        "summary": {"type": "string"},
        "report_markdown": {"type": "string",
                            "description": "The full report your contract requires."},
    }


def _schema(extra: dict[str, Any], required: list[str]) -> dict[str, Any]:
    properties = _base_properties()
    properties.update(extra)
    return {
        "type": "object",
        "properties": properties,
        "required": ["status", "blocking_reason", "summary", "report_markdown", *required],
        "additionalProperties": False,
    }


_FINDING = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "severity": {"type": "string",
                         "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]},
            "location": {"type": "string", "description": "file:line"},
            "evidence": {"type": "string"},
            "recommendation": {"type": "string"},
            "verification": {"type": "string"},
        },
        "required": ["title", "severity", "location", "evidence",
                     "recommendation", "verification"],
        "additionalProperties": False,
    },
}

RESULT_SCHEMAS: dict[str, dict[str, Any]] = {
    "architect": _schema({
        "plan_markdown": {"type": "string"},
        "adr_required": {"type": "boolean"},
        "adr_markdown": {"type": "string"},
        "model_overrides": {
            "type": "array",
            "description": "One entry per agent whose registry default you are "
                           "deviating from; omit agents left at their default.",
            "items": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "enum": list(AGENTS)},
                    "model": {"type": "string"},
                },
                "required": ["agent", "model"],
                "additionalProperties": False,
            },
        },
        "affected_files": {"type": "array", "items": {"type": "string"}},
    }, ["plan_markdown", "adr_required", "adr_markdown", "model_overrides", "affected_files"]),

    "developer": _schema({
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "commands_run": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"command": {"type": "string"},
                               "exit_code": {"type": "integer"}},
                "required": ["command", "exit_code"],
                "additionalProperties": False,
            },
        },
        "deviations": {"type": "array", "items": {"type": "string"}},
    }, ["files_changed", "commands_run", "deviations"]),

    "security": _schema({"findings": _FINDING,
                         "checks_performed": {"type": "array", "items": {"type": "string"}}},
                        ["findings", "checks_performed"]),

    "qa": _schema({
        "tests_executed": {"type": "integer"},
        "tests_passed": {"type": "integer"},
        "tests_failed": {"type": "integer"},
        "criteria_covered": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"criterion": {"type": "string"},
                               "tests": {"type": "array", "items": {"type": "string"}}},
                "required": ["criterion", "tests"],
                "additionalProperties": False,
            },
        },
        "findings": _FINDING,
    }, ["tests_executed", "tests_passed", "tests_failed", "criteria_covered", "findings"]),

    "performance": _schema({"findings": _FINDING}, ["findings"]),

    "reviewer": _schema({
        "decision": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES", "BLOCK"]},
        "verdicts": {
            "type": "array",
            "description": "One entry per review area you assessed.",
            "items": {
                "type": "object",
                "properties": {
                    "area": {"type": "string", "enum": list(REVIEW_AREAS)},
                    "verdict": {"type": "string"},
                },
                "required": ["area", "verdict"],
                "additionalProperties": False,
            },
        },
        "required_changes": {"type": "array", "items": {"type": "string"}},
    }, ["decision", "verdicts", "required_changes"]),

    "documentation": _schema({
        "files_touched": {"type": "array", "items": {"type": "string"}},
        "files_considered": {"type": "array", "items": {"type": "string"}},
    }, ["files_touched", "files_considered"]),

    # The project pass asks for more than a file list: "only what shipped
    # affected" is checkable only if the agent says what drove each change and
    # why it left the rest alone.
    "documentation-project": _schema({
        "project_type": {"type": "string",
                         "description": "What you concluded this project is, from the evidence."},
        "files_touched": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"},
                               "change": {"type": "string"},
                               "driven_by": {"type": "string",
                                             "description": "Shipped ticket id(s) that made this necessary."}},
                "required": ["path", "change", "driven_by"],
                "additionalProperties": False,
            },
        },
        "files_considered": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"},
                               "reason_left_alone": {"type": "string"}},
                "required": ["path", "reason_left_alone"],
                "additionalProperties": False,
            },
        },
        "inferred_docs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"},
                               "justification": {"type": "string"}},
                "required": ["path", "justification"],
                "additionalProperties": False,
            },
        },
        "examples_run": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"command": {"type": "string"},
                               "exit_code": {"type": "integer"}},
                "required": ["command", "exit_code"],
                "additionalProperties": False,
            },
        },
    }, ["project_type", "files_touched", "files_considered", "inferred_docs",
        "examples_run"]),

    "devops": _schema({
        "areas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"area": {"type": "string"},
                               "applicable": {"type": "boolean"},
                               "status": {"type": "string",
                                          "enum": ["pass", "fail", "not_applicable"]},
                               "evidence": {"type": "string"}},
                "required": ["area", "applicable", "status", "evidence"],
                "additionalProperties": False,
            },
        },
    }, ["areas"]),
}

REPORT_FILES = {
    "architect": "implementation-plan.md",
    "developer": "implementation-report.md",
    "security": "security-report.md",
    "qa": "qa-report.md",
    "performance": "performance-report.md",
    "reviewer": "review-decision.md",
    "documentation": "documentation-report.md",
    "devops": "devops-report.md",
}


# --------------------------------------------------------------------- run

def tools_for(agent: str) -> list[dict[str, Any]]:
    """An agent that may not write is not given the tools to try."""
    if WRITE_SCOPES.get(agent) is None:
        return [t for t in TOOLS if t["name"] in READ_ONLY_TOOLS]
    return list(TOOLS)


def run(agent: str, *, project: Project, root: Path, executor: Executor,
        user_prompt: str, registry: Registry, model: str | None = None,
        index: ContextIndex | None = None, shell: str = "sh",
        max_turns: int = 60, max_tokens: int = 32000,
        role: str | None = None, on_tool=None,
        backend: str = runners.API) -> AgentResult:
    """Run one agent to completion against one worktree.

    ``role`` selects the contract and result schema when an agent has more than
    one mission; it defaults to the agent's own name. Write scope, model and
    report filename always follow the *agent*, never the role.

    ``backend`` picks which runner makes the calls. Everything else here —
    contract, tools, write scope, result schema — is identical either way, which
    is the point: the backend changes who is billed, not what an agent may do.
    """
    role = role or agent
    system = load_contract(project, role)
    chosen = registry.validate(model) if model else registry.default_for(agent)
    effort = registry.effort_for(agent)

    box = ToolBox(agent, root, executor, index=index, shell=shell)
    runner = runners.build(backend, model=chosen, effort=effort, registry=registry)

    try:
        result, transcript = runner.agentic(
            system=system, user=user_prompt, tools=tools_for(agent),
            dispatch=box.dispatch, schema=RESULT_SCHEMAS[role],
            max_turns=max_turns, max_tokens=max_tokens, on_tool=on_tool,
        )
    except LLMError as exc:
        raise AgentError(f"{role} failed: {exc}") from exc

    return AgentResult(agent=agent, model=chosen, effort=runner.effort,
                       backend=runner.backend, result=result,
                       transcript=transcript, violations=list(box.violations))


def write_report(project: Project, ticket: str, agent: str, result: AgentResult) -> Path:
    """Persist the agent's report where the Reviewer and the approval gate read it."""
    directory = project.reports / ticket
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / REPORT_FILES[agent]
    body = result.result.get("report_markdown") or result.result.get("summary") or ""
    # Effort is omitted rather than defaulted when the backend has no such
    # control — a header claiming a setting that was never applied would make
    # two runs look comparable when they are not.
    effort = f" | effort {result.effort}" if result.effort else ""
    header = (f"<!-- {agent} | {result.backend} | model {result.model}{effort} "
              f"| {len(result.transcript)} tool call(s) -->\n\n")
    path.write_text(header + body.rstrip() + "\n", encoding="utf-8")
    return path
