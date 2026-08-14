"""Anthropic API adapter — the ``api`` backend.

One of two implementations of the ``Runner`` protocol in ``runner.py``; the
other, ``claude_code.py``, drives a Claude Code subscription instead. Everything
above either of them deals in ``structured()`` calls with a JSON Schema and gets
back a validated dict.

What is specific to this one is that the *model* is constrained to the schema
(``output_config.format``) and per-agent effort is a real request parameter.
Both are why this stays the preferred backend when a key is available.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

import anthropic

from .registry import Registry

DEFAULT_MAX_TOKENS = 16000

NO_CREDENTIALS = (
    "no Anthropic credentials found.\n"
    "  Set an API key:   export ANTHROPIC_API_KEY=sk-ant-...\n"
    "  or log in:        ant auth login\n"
    "The harness makes its own API calls, so it needs a credential of its own."
)


class LLMError(RuntimeError):
    pass


class Refused(LLMError):
    """The model's safety classifiers declined the request."""

    def __init__(self, category: str | None, explanation: str | None):
        self.category = category
        self.explanation = explanation
        super().__init__(f"request refused (category={category}): {explanation}")


def credentials_available() -> bool:
    """Best-effort pre-flight check.

    An unset ANTHROPIC_API_KEY does not mean there are no credentials — the SDK
    also resolves ANTHROPIC_AUTH_TOKEN and `ant auth login` profiles on disk.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    if os.environ.get("ANTHROPIC_PROFILE"):
        return True
    config_dir = os.environ.get("ANTHROPIC_CONFIG_DIR") or os.path.expanduser("~/.config/anthropic")
    return os.path.isdir(config_dir)


class LLM:
    """Thin wrapper: one structured, schema-validated call.

    This is the ``api`` backend of the ``Runner`` protocol in ``runner.py``; it
    satisfied that protocol before the protocol existed, which is why adding a
    second backend needed nothing here but this attribute.
    """

    backend = "api"

    def __init__(self, model: str, effort: str = "high", registry: Registry | None = None):
        if registry is not None:
            registry.validate(model)
        self.model = model
        self.effort = effort
        self._client: anthropic.Anthropic | None = None

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            # Zero-arg construction resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
            # or an `ant auth login` profile — an unset API key does not mean
            # "no credentials".
            self._client = anthropic.Anthropic()
        return self._client

    def structured(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict[str, Any]:
        """One request constrained to ``schema``. Streams so a long generation
        cannot trip the SDK's HTTP timeout."""
        try:
            with self.client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                thinking={"type": "adaptive"},
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                messages=[{"role": "user", "content": user}],
            ) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise LLMError(
                "authentication failed. Set ANTHROPIC_API_KEY, or run `ant auth login`."
            ) from exc
        except TypeError as exc:
            # The SDK raises a bare TypeError when it cannot resolve any
            # credential at all. Surface it as the configuration problem it is.
            if "authentication method" in str(exc):
                raise LLMError(NO_CREDENTIALS) from exc
            raise
        except anthropic.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"could not reach the API: {exc}") from exc

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise Refused(getattr(details, "category", None),
                          getattr(details, "explanation", None))

        if message.stop_reason == "max_tokens":
            raise LLMError(
                f"response hit max_tokens ({max_tokens}) and is truncated; "
                "rerun with a larger budget or split the input"
            )

        text = next((b.text for b in message.content if b.type == "text"), None)
        if not text:
            raise LLMError(f"no text content in response (stop_reason={message.stop_reason})")

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:  # pragma: no cover - schema makes this near-impossible
            raise LLMError(f"model returned unparseable JSON: {exc}") from exc

    def agentic(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        dispatch: Callable[[str, dict[str, Any]], tuple[str, bool]],
        schema: dict[str, Any],
        max_turns: int = 60,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        on_tool: Callable[[str, dict[str, Any], bool], None] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run a tool-use loop, ending in one schema-validated result.

        A hand-written loop rather than the SDK's tool runner: every tool call
        here crosses a boundary the harness has to police — an agent contract
        says which paths that agent may write, and commands run inside the
        ticket's container rather than on the host. Those checks live in
        ``dispatch``, and the loop exists to give them somewhere to stand.

        Returns the parsed final result and the transcript of tool calls, which
        is the evidence the Reviewer later reads.
        """
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        transcript: list[dict[str, Any]] = []

        for turn in range(max_turns):
            message = self._call(system=system, messages=messages, tools=tools,
                                 schema=schema, max_tokens=max_tokens)

            if message.stop_reason == "refusal":
                details = getattr(message, "stop_details", None)
                raise Refused(getattr(details, "category", None),
                              getattr(details, "explanation", None))
            if message.stop_reason == "max_tokens":
                raise LLMError(f"response hit max_tokens ({max_tokens}) on turn {turn + 1}")

            messages.append({"role": "assistant", "content": message.content})

            calls = [b for b in message.content if b.type == "tool_use"]
            if not calls:
                text = next((b.text for b in message.content if b.type == "text"), None)
                if not text:
                    raise LLMError(
                        f"agent stopped without a result (stop_reason={message.stop_reason})")
                try:
                    return json.loads(text), transcript
                except json.JSONDecodeError as exc:
                    raise LLMError(f"agent returned unparseable JSON: {exc}") from exc

            # Every result goes back in one user message: splitting them teaches
            # the model to stop making parallel calls.
            results = []
            for call in calls:
                output, is_error = dispatch(call.name, dict(call.input))
                transcript.append({"tool": call.name, "input": dict(call.input),
                                   "ok": not is_error, "output": output[:4000]})
                if on_tool is not None:
                    on_tool(call.name, dict(call.input), is_error)
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": output or "(no output)", "is_error": is_error})
            messages.append({"role": "user", "content": results})

        raise LLMError(
            f"agent did not finish within {max_turns} turns; the ticket is probably "
            "too large to implement as one unit")

    def _call(self, *, system: str, messages: list[dict[str, Any]],
              tools: list[dict[str, Any]], schema: dict[str, Any], max_tokens: int):
        try:
            with self.client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                thinking={"type": "adaptive"},
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                tools=tools,
                messages=messages,
            ) as stream:
                return stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise LLMError(
                "authentication failed. Set ANTHROPIC_API_KEY, or run `ant auth login`."
            ) from exc
        except TypeError as exc:
            if "authentication method" in str(exc):
                raise LLMError(NO_CREDENTIALS) from exc
            raise
        except anthropic.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"could not reach the API: {exc}") from exc
