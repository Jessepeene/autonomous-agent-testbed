"""DeepSeek provider -- serves TaskType.CODE_REPAIR only (see llm/router.py).

DeepSeek V4-Pro exposes an Anthropic-compatible endpoint, so this subclasses
AnthropicProvider and overrides only client construction (a different base_url
and a different credential); every request/response shape it inherits --
tool_use blocks, the streaming threshold, retry/refusal/schema-violation
handling -- is identical to the real Claude provider.

Deliberately routed to CODE_REPAIR (`converse()`, agentic tool-calling) and
never to CODE_GENERATION/CODE_REVIEW (`parse()`, strict `output_config.
format=json_schema` structured output): live testing showed DeepSeek's
Anthropic-compatible endpoint does not reliably honor that structured-output
contract -- it returned conversational prose instead of schema-conformant
JSON for a `generate_scaffold()`-shaped call, failing Pydantic validation.
Its tool-calling path, by contrast, was verified against a real broken-test-
suite fixture via the real WORKSPACE_SURGEON role and AgentRuntime loop: it
correctly diagnosed the bug, wrote a minimal correct fix, re-ran the suite,
and reported completion -- independently confirmed passing before this
routing was wired into the live system.
"""

from __future__ import annotations

import anthropic

from factory.llm.providers.anthropic_provider import AnthropicProvider
from factory.llm.providers.base import Provider, ProviderUnavailable

_BASE_URL = "https://api.deepseek.com/anthropic"


class DeepSeekProvider(AnthropicProvider):
    name = Provider.DEEPSEEK

    @property
    def available(self) -> bool:
        return self._settings.has_deepseek

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            key = self._settings.secret("DEEPSEEK_API_KEY")
            if not key:
                raise ProviderUnavailable(self.name, "DEEPSEEK_API_KEY is not set")
            self._client = anthropic.AsyncAnthropic(api_key=key, base_url=_BASE_URL)
        return self._client
