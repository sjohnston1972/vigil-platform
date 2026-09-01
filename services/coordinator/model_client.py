"""
Abstraction over the Claude model client used by the coordinator's /chat/stream loop.

`agent_loop.py` only depends on the `ModelClient` duck-type below (a `run_turn(...)`
async generator) — never on the `anthropic` package directly. That keeps the agent
loop unit-testable with a hand-written fake (see tests/test_agent_loop.py) and keeps
the real Azure AI Foundry wiring isolated to `FoundryModelClient`, which is never
constructed in tests.

Production auth: Azure Managed Identity via `azure.identity.aio.DefaultAzureCredential`
(consistent with the rest of the coordinator — see CLAUDE.md "Azure Key Vault Pattern").
No API key is read or required.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional, Protocol

logger = logging.getLogger(__name__)

# Azure AI resource scope for Managed Identity token acquisition.
_AZURE_COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"


@dataclass
class ToolCall:
    """One `tool_use` block from a Claude turn."""

    id: str
    name: str
    input: dict


@dataclass
class ModelTurn:
    """The accumulated result of one complete (non-partial) Claude turn."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0


class ModelBudgetExceededError(Exception):
    """Raised when the tenant's token budget is exhausted before/while calling the model."""


class ModelRateLimitedError(Exception):
    """Raised when the model provider rate-limits the request."""


class ModelUnavailableError(Exception):
    """Raised for any other fatal model-call failure (auth, network, 5xx, malformed response)."""


class ModelClient(Protocol):
    """
    Duck-typed contract every model client (real or fake) must satisfy.

    `run_turn` streams exactly one Claude turn. It must yield zero or more
    `{"type": "text_delta", "text": str}` events, in generation order, followed by
    exactly one final `{"type": "turn_complete", "turn": ModelTurn}` event.
    """

    def run_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        system: Optional[str] = None,
    ) -> AsyncIterator[dict[str, Any]]: ...


class FoundryModelClient:
    """
    Production `ModelClient` — Claude Sonnet 4.6 via Azure AI Foundry.

    Constructed lazily by `agent_loop._get_model_client()` on first real use, never
    at import time and never in tests, so importing this module never requires Azure
    credentials or `AZURE_FOUNDRY_ENDPOINT` / `AZURE_FOUNDRY_MODEL` to be set.

    NOT exercised against a live Foundry endpoint by the test suite — Azure AI Foundry
    is not reachable from CI/dev environments here. Verified only via the `ModelClient`
    contract (fakes in tests/test_agent_loop.py) and by constructing this class against
    a stub `azure_ad_token_provider` (see tests/test_model_client.py).
    """

    def __init__(self, endpoint: str, model: str):
        # Deferred import — keeps `anthropic` off the hot path for every test that
        # never touches the real model client.
        import anthropic
        from azure.identity.aio import DefaultAzureCredential

        self._model = model
        self._credential = DefaultAzureCredential()
        self._client = anthropic.AsyncAnthropicFoundry(
            base_url=endpoint,
            azure_ad_token_provider=self._azure_ad_token_provider,
        )

    async def _azure_ad_token_provider(self) -> str:
        token = await self._credential.get_token(_AZURE_COGNITIVE_SERVICES_SCOPE)
        return token.token

    async def run_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        system: Optional[str] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        import anthropic

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": messages,
            "tools": tools,
        }
        if system:
            kwargs["system"] = system

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    yield {"type": "text_delta", "text": text}
                final = await stream.get_final_message()
        except anthropic.RateLimitError as exc:
            raise ModelRateLimitedError(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            raise ModelUnavailableError(str(exc)) from exc
        except anthropic.AnthropicError as exc:
            raise ModelUnavailableError(str(exc)) from exc

        tool_calls = [
            ToolCall(id=block.id, name=block.name, input=block.input)
            for block in final.content
            if block.type == "tool_use"
        ]
        text = "".join(block.text for block in final.content if block.type == "text")

        yield {
            "type": "turn_complete",
            "turn": ModelTurn(
                text=text,
                tool_calls=tool_calls,
                stop_reason=final.stop_reason,
                input_tokens=final.usage.input_tokens,
                output_tokens=final.usage.output_tokens,
            ),
        }
