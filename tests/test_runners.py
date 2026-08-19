"""Tests for the model backends. No model calls and no network.

Cuts across every phase rather than belonging to one: both runners serve
spec review, chunking, execution and the documentation pass alike.

The Claude Code runner is exercised against a stand-in for the Agent SDK, which
is the only way to test the parts that matter without a subscription and a
logged-in CLI on the machine running the suite. What is worth pinning is not
that the SDK works but that the harness's own guarantees survive the trip
through it: the harness's tools are the only ones an agent can reach, the
transcript comes out in the shape the Reviewer reads, the result is schema-valid
or rejected, and the target repository's own Claude Code configuration stays out
of the agent's system prompt.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from ai_harness import agents, claude_code, runner
from ai_harness.agents import AgentResult
from ai_harness.claude_code import MCP_SERVER, SUBMIT_TOOL, ClaudeCodeRunner
from ai_harness.llm import LLM, LLMError
from ai_harness.paths import Project
from ai_harness.registry import Registry

SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "summary": {"type": "string"}},
    "required": ["status", "summary"],
    "additionalProperties": False,
}

TOOLS = [{
    "name": "read_file",
    "description": "Read a file.",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                     "required": ["path"]},
}]


@pytest.fixture
def project(tmp_path) -> Project:
    p = Project(tmp_path)
    p.ensure_layout()
    return p


@pytest.fixture(autouse=True)
def no_runner_env(monkeypatch):
    """The env var is a real input to selection, so tests must not inherit one."""
    monkeypatch.delenv(runner.ENV_VAR, raising=False)


# ------------------------------------------------------------ a stand-in SDK

def install_fake_sdk(monkeypatch, script):
    """A claude_agent_sdk that calls the tools named in ``script`` and stops.

    Each entry is a fully-qualified tool name as Claude Code would present it —
    ``mcp__harness__read_file`` for one of ours, a bare ``Bash`` for a built-in —
    so the permission callback is exercised exactly as it would be in a real run.
    """
    # Whether this machine has the CLI installed must not decide whether the
    # suite passes, so the lookup is pinned rather than left to the environment.
    monkeypatch.setattr(claude_code, "find_cli", lambda: "/stub/bin/claude")

    module = types.ModuleType("claude_agent_sdk")
    module.seen: list[tuple[str, bool]] = []
    module.options = None

    class Tool:
        def __init__(self, name, description, schema, handler):
            self.name, self.description = name, description
            self.schema, self.handler = schema, handler

    def tool(name, description, schema):
        return lambda handler: Tool(name, description, schema, handler)

    class Server:
        def __init__(self, name, tools):
            self.name = name
            self.tools = {t.name: t for t in tools}

    def create_sdk_mcp_server(*, name, tools=None, version="1.0.0"):
        return Server(name, tools or [])

    class Options:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class PermissionResultAllow:
        denied = False

        def __init__(self, updated_input=None):
            self.updated_input = updated_input

    class PermissionResultDeny:
        denied = True

        def __init__(self, message=""):
            self.message = message

    class Result:
        def __init__(self, terminal_reason):
            self.terminal_reason = terminal_reason

    async def query(*, prompt, options):
        module.options = options
        # The real SDK refuses a can_use_tool callback outside streaming mode.
        # The stand-in enforces it too: a fake that accepts what the real one
        # rejects is how a working test suite ships a broken runner.
        if options.can_use_tool is not None and isinstance(prompt, str):
            raise ValueError("can_use_tool callback requires streaming mode. "
                             "Please provide prompt as an AsyncIterable instead "
                             "of a string.")
        module.prompt_messages = [message async for message in prompt]
        server = options.mcp_servers[MCP_SERVER]
        for name, args in script:
            decision = await options.can_use_tool(name, args, None)
            module.seen.append((name, not decision.denied))
            if decision.denied:
                continue
            short = name.split("__")[-1] if name.startswith("mcp__") else name
            handler = server.tools[short].handler
            module.last_tool_result = await handler(args)
        yield Result("end_turn")

    module.tool = tool
    module.create_sdk_mcp_server = create_sdk_mcp_server
    module.ClaudeAgentOptions = Options
    module.PermissionResultAllow = PermissionResultAllow
    module.PermissionResultDeny = PermissionResultDeny
    module.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    return module


def recording_dispatch(calls):
    def dispatch(name, args):
        calls.append((name, args))
        return f"ran {name}", False
    return dispatch


# ------------------------------------------------------------- the protocol

def test_the_api_backend_already_satisfied_the_protocol():
    """LLM was written before the protocol existed. If this drifts, the seam
    stopped being a description of what was there and became a wrapper."""
    llm = LLM(model="claude-opus-5", effort="high")
    assert llm.backend == runner.API
    for method in ("structured", "agentic"):
        assert callable(getattr(llm, method))


def test_both_backends_expose_the_same_surface():
    api = LLM(model="claude-opus-5", effort="high")
    sub = ClaudeCodeRunner(model="claude-opus-5", effort="high")
    for name in ("backend", "model", "effort", "structured", "agentic"):
        assert hasattr(api, name) and hasattr(sub, name)


# -------------------------------------------------------------- selection

def test_the_api_backend_wins_when_both_are_available(monkeypatch):
    """Only the API path can constrain the model to the result schema and honour
    per-agent effort, so it is the better default when there is a choice."""
    monkeypatch.setitem(runner.AVAILABILITY, runner.API, lambda: (True, ""))
    monkeypatch.setitem(runner.AVAILABILITY, runner.CLAUDE_CODE, lambda: (True, ""))
    assert runner.select() == runner.API


def test_a_subscription_alone_is_enough(monkeypatch):
    monkeypatch.setitem(runner.AVAILABILITY, runner.API, lambda: (False, "no key"))
    monkeypatch.setitem(runner.AVAILABILITY, runner.CLAUDE_CODE, lambda: (True, ""))
    assert runner.select() == runner.CLAUDE_CODE


def test_an_explicit_choice_fails_rather_than_falling_back(monkeypatch):
    """Silently switching backends would change who is billed for the run."""
    monkeypatch.setitem(runner.AVAILABILITY, runner.API, lambda: (True, ""))
    monkeypatch.setitem(runner.AVAILABILITY, runner.CLAUDE_CODE,
                        lambda: (False, "the `claude` CLI is not on PATH"))
    with pytest.raises(runner.RunnerUnavailable) as exc:
        runner.select(runner.CLAUDE_CODE)
    assert "not on PATH" in str(exc.value)


def test_the_env_var_is_honoured(monkeypatch):
    monkeypatch.setenv(runner.ENV_VAR, runner.CLAUDE_CODE)
    monkeypatch.setitem(runner.AVAILABILITY, runner.API, lambda: (True, ""))
    monkeypatch.setitem(runner.AVAILABILITY, runner.CLAUDE_CODE, lambda: (True, ""))
    assert runner.select() == runner.CLAUDE_CODE


def test_no_backend_at_all_explains_both_options(monkeypatch):
    monkeypatch.setitem(runner.AVAILABILITY, runner.API, lambda: (False, "no key"))
    monkeypatch.setitem(runner.AVAILABILITY, runner.CLAUDE_CODE, lambda: (False, "no cli"))
    with pytest.raises(runner.RunnerUnavailable) as exc:
        runner.select()
    message = str(exc.value)
    assert "ANTHROPIC_API_KEY" in message and "Pro/Max" in message


def test_an_unknown_runner_name_is_rejected():
    with pytest.raises(runner.RunnerUnavailable):
        runner.select("openai")


# ---------------------------------------------------------------- fidelity

def test_the_subscription_backend_reports_no_effort_even_when_asked_for_one():
    """A report claiming an effort level the backend cannot apply would make two
    runs look comparable when they are not."""
    sub = ClaudeCodeRunner(model="claude-opus-5", effort="high")
    assert sub.requested_effort == "high"
    assert sub.effort == ""


def test_the_registry_gate_applies_to_both_backends(project):
    """The determinism gate is about which models are allowed, not which
    transport reaches them."""
    registry = Registry.load(project)
    with pytest.raises(ValueError):
        ClaudeCodeRunner(model="gpt-4", effort="", registry=registry)
    assert ClaudeCodeRunner(model="claude-opus-5", registry=registry).model == "claude-opus-5"


# ------------------------------------------------------------ finding the CLI

def test_path_wins_when_it_has_an_answer(monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which", lambda _n: "/usr/bin/claude")
    assert claude_code.find_cli() == "/usr/bin/claude"


def test_an_installed_cli_that_is_not_on_path_is_still_found(monkeypatch, tmp_path):
    """Installers write to directories they do not always add to PATH, and a
    shell started before the install never sees it either. Telling someone to
    install what they already have sends them to the wrong fix."""
    binary = tmp_path / "claude.exe"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(claude_code.shutil, "which", lambda _n: None)
    monkeypatch.setattr(claude_code, "CLI_LOCATIONS", (tmp_path / "absent", binary))

    assert claude_code.find_cli() == str(binary)
    assert runner.claude_code_available()[0] is True


def test_a_cli_that_is_nowhere_is_reported_as_missing(monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which", lambda _n: None)
    monkeypatch.setattr(claude_code, "CLI_LOCATIONS", ())
    assert claude_code.find_cli() is None

    ok, why = runner.claude_code_available()
    assert ok is False and "not found" in why


def test_a_missing_cli_stops_the_run_before_the_sdk_is_asked(monkeypatch):
    install_fake_sdk(monkeypatch, [])
    monkeypatch.setattr(claude_code, "find_cli", lambda: None)
    with pytest.raises(LLMError) as exc:
        ClaudeCodeRunner(model="claude-opus-5").structured(
            system="s", user="u", schema=SCHEMA)
    assert "Claude Code CLI was not found" in str(exc.value)


def test_the_user_turn_is_streamed_not_passed_as_a_string(monkeypatch):
    """The SDK rejects a can_use_tool callback outside streaming mode, and that
    callback is the boundary keeping agents off the built-in tools. Streaming is
    therefore load-bearing, not stylistic."""
    module = install_fake_sdk(monkeypatch, [
        (f"mcp__{MCP_SERVER}__{SUBMIT_TOOL}", {"status": "complete", "summary": "ok"}),
    ])
    ClaudeCodeRunner(model="claude-opus-5").structured(
        system="s", user="review this", schema=SCHEMA)

    assert module.options.can_use_tool is not None
    assert module.prompt_messages == [{
        "type": "user",
        "message": {"role": "user", "content": "review this"},
        "parent_tool_use_id": None,
    }]


def test_the_resolved_cli_path_is_handed_to_the_sdk(monkeypatch):
    """Finding it is not enough — the SDK spawns the CLI and does its own PATH
    lookup, which is exactly what failed."""
    module = install_fake_sdk(monkeypatch, [
        (f"mcp__{MCP_SERVER}__{SUBMIT_TOOL}", {"status": "complete", "summary": "ok"}),
    ])
    monkeypatch.setattr(claude_code, "find_cli", lambda: "/elsewhere/claude.exe")
    ClaudeCodeRunner(model="claude-opus-5").structured(
        system="s", user="u", schema=SCHEMA)
    assert module.options.cli_path == "/elsewhere/claude.exe"


def test_a_missing_sdk_is_a_configuration_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    sub = ClaudeCodeRunner(model="claude-opus-5")
    with pytest.raises(LLMError) as exc:
        sub.structured(system="s", user="u", schema=SCHEMA)
    assert "claude-agent-sdk" in str(exc.value)


def test_an_sdk_without_permission_callbacks_is_refused(monkeypatch):
    """Deny-by-default is the boundary. An SDK too old to enforce it must fail
    loudly rather than run the agents with no boundary at all."""
    module = types.ModuleType("claude_agent_sdk")
    module.query = module.tool = module.create_sdk_mcp_server = lambda *a, **k: None
    module.ClaudeAgentOptions = object
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    with pytest.raises(LLMError) as exc:
        ClaudeCodeRunner(model="claude-opus-5").structured(
            system="s", user="u", schema=SCHEMA)
    assert "PermissionResultAllow" in str(exc.value)


# ------------------------------------------------------- the wiring itself

def test_harness_tools_reach_the_toolbox_and_land_in_the_transcript(monkeypatch):
    calls: list = []
    install_fake_sdk(monkeypatch, [
        (f"mcp__{MCP_SERVER}__read_file", {"path": "a.py"}),
        (f"mcp__{MCP_SERVER}__{SUBMIT_TOOL}", {"status": "complete", "summary": "done"}),
    ])
    result, transcript = ClaudeCodeRunner(model="claude-opus-5").agentic(
        system="contract", user="go", tools=TOOLS,
        dispatch=recording_dispatch(calls), schema=SCHEMA)

    assert calls == [("read_file", {"path": "a.py"})]
    assert result == {"status": "complete", "summary": "done"}
    assert transcript == [{"tool": "read_file", "input": {"path": "a.py"},
                           "ok": True, "output": "ran read_file"}]


def test_built_in_tools_are_denied(monkeypatch):
    """Claude Code's own Write would bypass the agent's write scope, its own Bash
    would run outside the ticket's container. Neither is reachable."""
    calls: list = []
    module = install_fake_sdk(monkeypatch, [
        ("Bash", {"command": "rm -rf /"}),
        ("Write", {"file_path": "src/app.py"}),
        (f"mcp__{MCP_SERVER}__{SUBMIT_TOOL}", {"status": "complete", "summary": "done"}),
    ])
    ClaudeCodeRunner(model="claude-opus-5").agentic(
        system="contract", user="go", tools=TOOLS,
        dispatch=recording_dispatch(calls), schema=SCHEMA)

    assert ("Bash", False) in module.seen
    assert ("Write", False) in module.seen
    assert calls == []


def test_a_result_that_fails_the_schema_is_rejected_and_can_be_retried(monkeypatch):
    """The model does not enforce the schema on this path, so the harness does —
    and hands back something the agent can act on rather than failing the run."""
    module = install_fake_sdk(monkeypatch, [
        (f"mcp__{MCP_SERVER}__{SUBMIT_TOOL}", {"status": "complete"}),      # no summary
        (f"mcp__{MCP_SERVER}__{SUBMIT_TOOL}", {"status": "complete", "summary": "ok"}),
    ])
    result, _ = ClaudeCodeRunner(model="claude-opus-5").agentic(
        system="contract", user="go", tools=[],
        dispatch=recording_dispatch([]), schema=SCHEMA)

    assert result == {"status": "complete", "summary": "ok"}
    assert module.last_tool_result["content"][0]["text"] == "accepted"


def test_structured_calls_work_without_any_tools(monkeypatch):
    """review-specs and chunk-specs are whole phases that never use a tool loop,
    so the no-tools path has to work on this backend too."""
    module = install_fake_sdk(monkeypatch, [
        (f"mcp__{MCP_SERVER}__{SUBMIT_TOOL}", {"status": "complete", "summary": "ok"}),
    ])
    result = ClaudeCodeRunner(model="claude-opus-5").structured(
        system="prompt", user="review this", schema=SCHEMA)

    assert result == {"status": "complete", "summary": "ok"}

    # No allowed_tools entry: one there auto-approves the tool before
    # can_use_tool is consulted, which would shadow the callback and leave the
    # allowlist as the real gate. The callback has to be the only one.
    assert getattr(module.options, "allowed_tools", None) is None
    permit = module.options.can_use_tool
    assert asyncio.run(permit(f"mcp__{MCP_SERVER}__{SUBMIT_TOOL}", {}, None)).denied is False
    assert asyncio.run(permit("Bash", {"command": "rm -rf /"}, None)).denied is True


def test_ending_without_a_result_is_an_error(monkeypatch):
    install_fake_sdk(monkeypatch, [(f"mcp__{MCP_SERVER}__read_file", {"path": "a.py"})])
    with pytest.raises(LLMError) as exc:
        ClaudeCodeRunner(model="claude-opus-5").agentic(
            system="contract", user="go", tools=TOOLS,
            dispatch=recording_dispatch([]), schema=SCHEMA)
    assert SUBMIT_TOOL in str(exc.value)


def test_the_target_repos_own_claude_config_is_not_loaded(monkeypatch):
    """The contract is the agent's whole system prompt. Left to itself the SDK
    would also load the target repo's CLAUDE.md, skills and settings, making one
    project's agents behave differently from another's."""
    module = install_fake_sdk(monkeypatch, [
        (f"mcp__{MCP_SERVER}__{SUBMIT_TOOL}", {"status": "complete", "summary": "done"}),
    ])
    ClaudeCodeRunner(model="claude-opus-5").agentic(
        system="the contract", user="go", tools=TOOLS,
        dispatch=recording_dispatch([]), schema=SCHEMA)

    assert module.options.setting_sources == []
    assert module.options.system_prompt == "the contract"
    assert "Bash" in module.options.disallowed_tools


# -------------------------------------------------------------- reporting

def test_the_report_header_names_the_backend_and_omits_absent_effort(project):
    """Reports are the evidence a human approves on, so which backend produced
    one — and whether an effort level was really applied — has to be on the page."""
    for backend, effort, expected in ((runner.API, "high", "api | model claude-opus-5 | effort high"),
                                      (runner.CLAUDE_CODE, "", "claude-code | model claude-opus-5 |")):
        result = AgentResult(agent="security", model="claude-opus-5", effort=effort,
                             backend=backend, result={"report_markdown": "# Report"})
        header = agents.write_report(project, "T-001", "security",
                                     result).read_text(encoding="utf-8")
        assert expected in header
        if backend == runner.CLAUDE_CODE:
            assert "effort" not in header


def test_the_backend_travels_with_the_result(project, monkeypatch):
    """pipeline.py records the runner on task.agent_completed, which is only
    truthful if agents.run reports what actually answered."""
    monkeypatch.setattr(runner, "build", lambda backend, **kw: _StubRunner(backend, **kw))
    result = agents.run(
        "reviewer", project=project, root=project.root, executor=None,
        user_prompt="go", registry=Registry.load(project),
        backend=runner.CLAUDE_CODE)
    assert result.backend == runner.CLAUDE_CODE
    assert result.effort == ""


class _StubRunner:
    def __init__(self, backend, *, model, effort, registry=None):
        self.backend, self.model = backend, model
        self.effort = "" if backend == runner.CLAUDE_CODE else effort

    def agentic(self, **kwargs):
        return {"status": "complete", "summary": "", "blocking_reason": "",
                "report_markdown": "", "decision": "APPROVE", "verdicts": [],
                "required_changes": []}, []
