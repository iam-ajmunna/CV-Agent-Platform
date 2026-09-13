"""cv_agent.llm.provider — LLM provider protocol.

Defines the provider protocol consumed by LLMGateway.
Concrete adapters (*Adapter) implement this protocol inside cv_agent/llm/.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from cv_agent.llm.contracts import LLMRequest, LLMResponse, LLMStreamEvent, ModelProfile


class LLMProvider(Protocol):
    """Provider protocol boundary (ADR-0002 §15).

    Concrete implementations (*Adapter) isolate all vendor SDKs and network
    protocols within cv_agent/llm/.
    """

    provider_id: str

    def complete(
        self,
        request: LLMRequest,
        model: ModelProfile,
    ) -> LLMResponse:
        """Execute completion and return normalized LLMResponse."""
        ...

    def stream(
        self,
        request: LLMRequest,
        model: ModelProfile,
    ) -> Iterator[LLMStreamEvent]:
        """Execute streaming and yield typed LLMStreamEvents."""
        ...
