"""Provider adapter interface and registry.

Adapters translate one chat call into a vendor HTTP API. They never touch the database and never
see more than the one API key they are handed for the call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class Completion:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class ProviderAdapter(Protocol):
    name: str

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        api_key: str | None,
        base_url: str | None,
        params: dict[str, Any],
        timeout: float,
    ) -> Completion: ...


_REGISTRY: dict[str, Callable[[], ProviderAdapter]] = {}


def register(name: str, factory: Callable[[], ProviderAdapter]) -> None:
    _REGISTRY[name] = factory


def get_adapter(name: str) -> ProviderAdapter:
    _load_builtin()
    if name not in _REGISTRY:
        raise ValueError(f"unknown provider {name!r}; known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def known_providers() -> list[str]:
    _load_builtin()
    return sorted(_REGISTRY)


_loaded = False


def _load_builtin() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from agenthub.providers import anthropic, google, mock, openai  # noqa: F401


# USD per 1M tokens (input, output). Static estimate; real invoices may differ.
# Unknown models are estimated at 0 and flagged in the trace.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.3, 2.5),
    "mock": (1.0, 2.0),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> tuple[float, bool]:
    """Return (usd, known_price)."""
    for prefix in sorted(PRICES, key=len, reverse=True):
        if model.startswith(prefix):
            pin, pout = PRICES[prefix]
            return (input_tokens * pin + output_tokens * pout) / 1_000_000, True
    return 0.0, False
