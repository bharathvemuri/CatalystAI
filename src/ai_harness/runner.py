"""Choosing what actually makes the model calls.

The harness can reach a model two ways, and they are not two credentials for
one backend — they are different products with different terms:

``api``
    Talks to ``/v1/messages`` with an Anthropic API key. The model itself is
    constrained to the result schema (``output_config.format``) and per-agent
    effort applies. Full fidelity; billed per token.

``claude-code``
    Drives the Claude Agent SDK, which runs whatever ``claude`` is already
    logged in as. A Claude Pro/Max subscription therefore works with no API
    key. Schema constraint is enforced by the harness rather than the model,
    and effort has no equivalent knob.

Everything above this module asks for a ``Runner`` and never learns which one
answered, because both satisfy the same two-method protocol — ``LLM`` already
did, which is why the API path needed no rewrite to fit here.

A note on the subscription path, since it is easy to get wrong. Anthropic's
Agent SDK terms do not let a third-party product offer claude.ai login or
subscription rate limits to *its own* users. That is not what this backend
does: it invokes the ``claude`` on the developer's own machine, under their own
login, the way any local tool invokes a CLI the user already has. Shipping a
hosted service on top of it would be a different question, and not one this
module answers.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Protocol

from .registry import Registry

API = "api"
CLAUDE_CODE = "claude-code"
BACKENDS = (API, CLAUDE_CODE)

ENV_VAR = "AI_HARNESS_RUNNER"

NOTHING_AVAILABLE = """\
no way to reach a model was found. The harness makes its own calls, so it needs
one of these:

  An Anthropic API key      export ANTHROPIC_API_KEY=sk-ant-...
                            or log in with `ant auth login`

  A Claude Pro/Max plan     install the Claude Code CLI and run `claude` once to
                            log in, then: pip install "ai-harness[claude-code]"

Pick one explicitly with --runner {api,claude-code} once both are set up."""


class RunnerUnavailable(RuntimeError):
    """No backend can serve this run, or the requested one cannot."""


class Runner(Protocol):
    """What the pipeline needs from a model backend.

    ``LLM`` conforms without modification; ``ClaudeCodeRunner`` was written to.
    Both return the same shapes, so agent contracts, result schemas, write
    scopes, gates and reports are identical whichever one runs.
    """

    backend: str
    model: str
    effort: str

    def structured(self, *, system: str, user: str, schema: dict[str, Any],
                   max_tokens: int = ...) -> dict[str, Any]:
        """One request constrained to ``schema``."""

    def agentic(self, *, system: str, user: str, tools: list[dict[str, Any]],
                dispatch: Callable[[str, dict[str, Any]], tuple[str, bool]],
                schema: dict[str, Any], max_turns: int = ..., max_tokens: int = ...,
                on_tool: Callable[[str, dict[str, Any], bool], None] | None = ...,
                ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run a tool-use loop, ending in one schema-validated result."""


# ------------------------------------------------------------- availability

def api_available() -> tuple[bool, str]:
    from .llm import credentials_available

    if credentials_available():
        return True, ""
    return False, "no ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or `ant auth login` profile"


def claude_code_available() -> tuple[bool, str]:
    """Both halves are needed: the SDK is a library around the CLI, not a
    replacement for it, so a missing ``claude`` fails at first call rather
    than at import."""
    from .claude_code import find_cli

    if find_cli() is None:
        return False, ("the Claude Code CLI was not found on PATH or in the "
                       "usual install locations")
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False, ('the claude-agent-sdk package is not installed '
                       '(pip install "ai-harness[claude-code]")')
    return True, ""


AVAILABILITY: dict[str, Callable[[], tuple[bool, str]]] = {
    API: api_available,
    CLAUDE_CODE: claude_code_available,
}


def select(preference: str | None = None) -> str:
    """Resolve the backend to use, or explain why none is usable.

    An explicit choice is honoured or it fails — silently falling back to the
    other backend would change what the run costs and how it is billed, which
    is not a decision to make on someone's behalf.
    """
    choice = preference or os.environ.get(ENV_VAR) or None
    if choice:
        if choice not in BACKENDS:
            raise RunnerUnavailable(
                f"unknown runner {choice!r}; known: {', '.join(BACKENDS)}")
        ok, why = AVAILABILITY[choice]()
        if not ok:
            raise RunnerUnavailable(f"--runner {choice} was asked for, but {why}.")
        return choice

    # The API path is preferred when both are set up: it is the only one that
    # can constrain the model to the result schema and honour per-agent effort.
    for candidate in BACKENDS:
        ok, _why = AVAILABILITY[candidate]()
        if ok:
            return candidate
    raise RunnerUnavailable(NOTHING_AVAILABLE)


def describe(backend: str) -> str:
    """One line for the plan and the event log."""
    if backend == API:
        return "Anthropic API (schema-constrained, per-agent effort)"
    if backend == CLAUDE_CODE:
        return "Claude Code / Agent SDK (your `claude` login; no effort control)"
    return backend


# ------------------------------------------------------------ construction

def build(backend: str, *, model: str, effort: str,
          registry: Registry | None = None) -> Runner:
    """Construct the chosen backend for one agent's model and effort."""
    if backend == API:
        from .llm import LLM

        return LLM(model=model, effort=effort, registry=registry)
    if backend == CLAUDE_CODE:
        from .claude_code import ClaudeCodeRunner

        return ClaudeCodeRunner(model=model, effort=effort, registry=registry)
    raise RunnerUnavailable(f"unknown runner {backend!r}; known: {', '.join(BACKENDS)}")


def add_argument(parser) -> None:
    """The ``--runner`` flag, shared by every command that calls a model."""
    parser.add_argument("--runner", choices=list(BACKENDS), default=None,
                        help="which backend makes the model calls "
                             f"(default: auto-detect; ${ENV_VAR} also honoured)")
