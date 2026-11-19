"""Deterministic provider for tests and offline demos.

Behaviour is controlled through `model_params` on the agent:
- "fail": true            -> raises ProviderError (simulates an unavailable agent)
- "reply": "text"         -> fixed reply
- "persona": "name"       -> prefix replies with the persona
Otherwise it echoes the last user message and reports which context version it saw, which lets
tests check that two agents read the same context while producing independent output.
"""

from __future__ import annotations

import re
from typing import Any

from agenthub.errors import ProviderError
from agenthub.providers.base import Completion, Message, register


class MockAdapter:
    name = "mock"

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
    ) -> Completion:
        if params.get("fail"):
            raise ProviderError("mock provider configured to fail")
        last = messages[-1].content if messages else ""
        m = re.search(r"context_version:\s*(\d+)", system)
        seen = m.group(1) if m else "none"
        persona = params.get("persona", model)
        text = params.get("reply") or f"[{persona}] ctx={seen} turns={len(messages)} :: {last[-300:]}"
        return Completion(
            text=text,
            input_tokens=(len(system) + sum(len(x.content) for x in messages)) // 4,
            output_tokens=len(text) // 4,
        )


register("mock", MockAdapter)
