"""Claude Code adapter — the subscription-friendly backend.

Drives the Claude Agent SDK, which runs the ``claude`` CLI already logged in on
this machine. A Claude Pro/Max plan therefore works here with no API key.

Two decisions shape everything below.

**The harness keeps its own tools.** The Agent SDK ships Read/Write/Edit/Bash,
but none of them know about this harness's boundaries: an agent's write scope,
the ticket worktree it may not escape, or the container its commands have to run
inside. Rather than re-implement those checks against a second tool vocabulary,
the same ``ToolBox`` the API path uses is exposed as an in-process MCP server and
every built-in tool is denied. The cost is that agents give up Claude Code's
better file tooling; the gain is that both backends run the identical tools under
the identical enforcement and emit the identical transcript, so a report means
the same thing whichever one produced it.

**The result schema is enforced here, not by the model.** There is no
``output_config.format`` on this path, so the agent finishes by calling a
``submit_result`` tool whose input schema *is* the role's result schema. The
harness validates the call and hands back a rejection the agent can act on, so a
malformed result costs a turn instead of the run. What reaches ``pipeline.py``
is a validated dict either way.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any, Callable

import jsonschema

from .llm import LLMError
from .registry import Registry

MCP_SERVER = "harness"
SUBMIT_TOOL = "submit_result"

DEFAULT_MAX_TURNS = 60

NOT_INSTALLED = (
    "the Claude Code runner needs the claude-agent-sdk package and a logged-in "
    'Claude Code CLI.\n  pip install "ai-harness[claude-code]"\n  claude  '
    "(once, to log in)"
)

# Deny-by-default in ``can_use_tool`` is the real boundary; this list only tells
# the model not to reach for them, so a denial is rare rather than routine.
BUILTIN_TOOLS = (
    "Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "NotebookEdit",
    "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite",
)

SUBMIT_DESCRIPTION = (
    "Return your final result and end your turn. Call this exactly once, when "
    "your contract's Required Output is complete. The call is rejected if it "
    "does not match the schema, and you may correct it and call again."
)


NO_CLI = (
    "the Claude Code CLI was not found. Install it, then run `claude` once to "
    "log in:\n  npm install -g @anthropic-ai/claude-code\nIf it is already "
    "installed, its directory is not on this process's PATH — open a new "
    "terminal, or put the binary's directory on PATH."
)

# Where the installers put it when PATH does not say so. Kept alongside the
# PATH lookup rather than instead of it: PATH is still the answer whenever it
# has one, and these are only consulted when it does not.
CLI_LOCATIONS = (
    Path.home() / ".local" / "bin" / "claude.exe",          # native, Windows
    Path.home() / ".local" / "bin" / "claude",              # native, POSIX
    Path.home() / ".claude" / "local" / "claude",
    Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd",   # npm -g, Windows
    Path("/usr/local/bin/claude"),
    Path("/opt/homebrew/bin/claude"),
)


def find_cli() -> str | None:
    """Locate the Claude Code CLI: PATH first, then the standard install paths.

    The same shape as ``execution.posix_shell()``, for the same reason. A tool
    the user has already installed should not look missing because the shell
    that launched the harness predates the install, or because an installer
    wrote to a directory it never added to PATH. Telling someone to install
    what they already have is a bad error message, and it sends them to the
    wrong fix.
    """
    found = shutil.which("claude")
    if found:
        return found
    for candidate in CLI_LOCATIONS:
        if candidate.is_file():
            return str(candidate)
    return None


def _qualified(name: str) -> str:
    return f"mcp__{MCP_SERVER}__{name}"


class ClaudeCodeRunner:
    """Satisfies the same protocol as ``LLM``, over the Agent SDK.

    ``effort`` is accepted so the two backends construct alike, but the SDK has
    no equivalent control. It is reported as empty rather than echoed back,
    because a report claiming an effort level that was never applied is worse
    than one that admits the backend has none.
    """

    backend = "claude-code"

    def __init__(self, model: str, effort: str = "", registry: Registry | None = None):
        if registry is not None:
            registry.validate(model)
        self.model = model
        self.requested_effort = effort
        self.effort = ""

    # ------------------------------------------------------------- the loop

    def structured(self, *, system: str, user: str, schema: dict[str, Any],
                   max_tokens: int = 0) -> dict[str, Any]:
        """One schema-validated result, no tools but the submit call.

        ``max_tokens`` has no equivalent on this backend and is ignored; the
        parameter exists so callers do not have to know which runner they hold.
        """
        result, _transcript = self._converse(
            system=system, user=user, tools=[], dispatch=None,
            schema=schema, max_turns=6, on_tool=None)
        return result

    def agentic(self, *, system: str, user: str, tools: list[dict[str, Any]],
                dispatch: Callable[[str, dict[str, Any]], tuple[str, bool]],
                schema: dict[str, Any], max_turns: int = DEFAULT_MAX_TURNS,
                max_tokens: int = 0,
                on_tool: Callable[[str, dict[str, Any], bool], None] | None = None,
                ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run the harness's tools through Claude Code's loop.

        Returns the parsed result and the transcript of tool calls, in the same
        shape ``LLM.agentic`` returns, because the Reviewer reads it as evidence
        and should not be able to tell the backends apart.
        """
        return self._converse(system=system, user=user, tools=tools,
                              dispatch=dispatch, schema=schema,
                              max_turns=max_turns, on_tool=on_tool)

    def _converse(self, *, system: str, user: str, tools: list[dict[str, Any]],
                  dispatch: Callable[[str, dict[str, Any]], tuple[str, bool]] | None,
                  schema: dict[str, Any], max_turns: int,
                  on_tool: Callable[[str, dict[str, Any], bool], None] | None,
                  ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:  # pragma: no cover - the harness is synchronous throughout
            raise LLMError("the Claude Code runner cannot be started from inside a "
                           "running event loop")

        return asyncio.run(self._converse_async(
            system=system, user=user, tools=tools, dispatch=dispatch,
            schema=schema, max_turns=max_turns, on_tool=on_tool))

    async def _converse_async(self, *, system, user, tools, dispatch, schema,
                              max_turns, on_tool):
        sdk = _import_sdk()
        cli = find_cli()
        if cli is None:
            raise LLMError(NO_CLI)

        transcript: list[dict[str, Any]] = []
        captured: dict[str, Any] = {}

        server = sdk.create_sdk_mcp_server(
            name=MCP_SERVER,
            tools=[
                *(self._wrap(sdk, spec, dispatch, transcript, on_tool)
                  for spec in tools),
                self._submit_tool(sdk, schema, captured),
            ],
        )
        allowed = [_qualified(spec["name"]) for spec in tools] + [_qualified(SUBMIT_TOOL)]

        options = sdk.ClaudeAgentOptions(
            model=self.model,
            # Passed explicitly rather than left to the SDK's own lookup, which
            # searches PATH — the whole point of find_cli() is the case where
            # PATH does not have it.
            cli_path=cli,
            system_prompt=system,
            mcp_servers={MCP_SERVER: server},
            allowed_tools=allowed,
            disallowed_tools=list(BUILTIN_TOOLS),
            can_use_tool=_only_harness_tools(sdk, set(allowed)),
            # The agent's contract is its whole system prompt. Without this the
            # SDK would also load the *target repository's* CLAUDE.md, skills
            # and settings, quietly making one project's agents behave
            # differently from another's.
            setting_sources=[],
            max_turns=max_turns,
        )

        terminal = ""
        try:
            async for message in sdk.query(prompt=user, options=options):
                reason = getattr(message, "terminal_reason", None)
                if reason:
                    terminal = str(reason)
        except LLMError:
            raise
        except Exception as exc:
            # The SDK reaches a CLI subprocess, so failures arrive as anything
            # from a missing binary to a transport decode error. Every one of
            # them is the same thing to the pipeline: this agent did not run.
            raise LLMError(f"the Claude Code runner failed: "
                           f"{type(exc).__name__}: {exc}") from exc

        if SUBMIT_TOOL not in captured:
            raise LLMError(
                f"the agent ended its turn without calling {SUBMIT_TOOL}"
                + (f" (terminal reason: {terminal})" if terminal else "")
                + ". That usually means it ran out of turns before finishing.")
        return captured[SUBMIT_TOOL], transcript

    # -------------------------------------------------------------- tools

    def _wrap(self, sdk, spec: dict[str, Any], dispatch, transcript, on_tool):
        """Expose one harness tool over MCP, unchanged.

        ``dispatch`` is synchronous and can block for the length of a container
        build, so it runs on a worker thread — blocking the event loop here
        would stall the transport that is pumping the CLI subprocess.
        """
        name = spec["name"]

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            payload = dict(args)
            output, is_error = await asyncio.to_thread(dispatch, name, payload)
            transcript.append({"tool": name, "input": payload,
                               "ok": not is_error, "output": output[:4000]})
            if on_tool is not None:
                on_tool(name, payload, is_error)
            return {"content": [{"type": "text", "text": output or "(no output)"}],
                    "is_error": is_error}

        return sdk.tool(name, spec["description"], spec["input_schema"])(handler)

    def _submit_tool(self, sdk, schema: dict[str, Any], captured: dict[str, Any]):
        """The result channel, and the only place the schema is enforced."""

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            candidate = dict(args)
            try:
                jsonschema.validate(instance=candidate, schema=schema)
            except jsonschema.ValidationError as exc:
                where = "/".join(str(p) for p in exc.absolute_path) or "(root)"
                return {"content": [{"type": "text", "text":
                        f"rejected: {where}: {exc.message}. Correct it and "
                        f"call {SUBMIT_TOOL} again."}],
                        "is_error": True}
            captured[SUBMIT_TOOL] = candidate
            return {"content": [{"type": "text", "text": "accepted"}]}

        return sdk.tool(SUBMIT_TOOL, SUBMIT_DESCRIPTION, schema)(handler)


def _only_harness_tools(sdk, allowed: set[str]):
    """Deny by default.

    An allowlist of names rather than a denylist, so a built-in tool added to a
    future Claude Code release cannot quietly acquire write access that bypasses
    the agent's contract.
    """

    async def can_use_tool(tool_name: str, input_data: dict[str, Any], context):
        if tool_name in allowed:
            return sdk.PermissionResultAllow(updated_input=input_data)
        return sdk.PermissionResultDeny(
            message=f"{tool_name} is not available to harness agents; use the "
                    f"mcp__{MCP_SERVER}__* tools your contract lists.")

    return can_use_tool


def _import_sdk():
    """The SDK is optional, so its absence is a configuration error, not a crash."""
    try:
        import claude_agent_sdk as sdk
    except ImportError as exc:
        raise LLMError(NOT_INSTALLED) from exc

    missing = [name for name in ("query", "tool", "create_sdk_mcp_server",
                                 "ClaudeAgentOptions", "PermissionResultAllow",
                                 "PermissionResultDeny")
               if not hasattr(sdk, name)]
    if missing:
        raise LLMError(
            f"this claude-agent-sdk is missing {', '.join(missing)}. The harness "
            "needs a version with in-process MCP tools and permission callbacks; "
            "upgrade with: pip install -U claude-agent-sdk")
    return sdk
