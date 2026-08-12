"""Anthropic API adapter.

The only place in the harness that knows about a model provider. Everything
above it deals in ``structured()`` calls with a JSON Schema and gets back a
validated dict.
"""

from __future__ import annotations

import json
import os
from typing import Any

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
    """Thin wrapper: one structured, schema-validated call."""

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
