"""Anthropic Messages API."""

from __future__ import annotations

from typing import Any

from agenthub.providers._http import post_json
from agenthub.providers.base import Completion, Message, register


class AnthropicAdapter:
    name = "anthropic"

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
        url = (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key or "",
            "anthropic-version": "2023-06-01",
        }
        body = {
            "model": model,
            "system": system,
            "max_tokens": int(params.get("max_tokens", 2048)),
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if "temperature" in params:
            body["temperature"] = params["temperature"]
        data = await post_json(url, headers, body, timeout)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage") or {}
        return Completion(
            text=text,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )


register("anthropic", AnthropicAdapter)
