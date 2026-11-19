"""OpenAI Chat Completions, and any OpenAI-compatible server (vLLM, Ollama, LM Studio, OpenRouter)."""

from __future__ import annotations

from typing import Any

from agenthub.providers._http import post_json
from agenthub.providers.base import Completion, Message, register


class OpenAIAdapter:
    name = "openai"
    default_base = "https://api.openai.com/v1"

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
        url = (base_url or self.default_base).rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
            **{k: v for k, v in params.items() if k in {"temperature", "max_tokens", "top_p"}},
        }
        data = await post_json(url, headers, body, timeout)
        usage = data.get("usage") or {}
        return Completion(
            text=data["choices"][0]["message"].get("content") or "",
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )


class OpenAICompatibleAdapter(OpenAIAdapter):
    name = "openai_compatible"


register("openai", OpenAIAdapter)
register("openai_compatible", OpenAICompatibleAdapter)
