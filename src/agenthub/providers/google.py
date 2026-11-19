"""Google Gemini API (generateContent)."""

from __future__ import annotations

from typing import Any

from agenthub.providers._http import post_json
from agenthub.providers.base import Completion, Message, register


class GoogleAdapter:
    name = "google"

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
        base = (base_url or "https://generativelanguage.googleapis.com").rstrip("/")
        url = f"{base}/v1beta/models/{model}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": api_key or ""}
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [
                {"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
                for m in messages
            ],
        }
        gen = {k: v for k, v in params.items() if k in {"temperature", "topP"}}
        if "max_tokens" in params:
            gen["maxOutputTokens"] = params["max_tokens"]
        if gen:
            body["generationConfig"] = gen
        data = await post_json(url, headers, body, timeout)
        cands = data.get("candidates") or []
        parts = (cands[0].get("content") or {}).get("parts", []) if cands else []
        usage = data.get("usageMetadata") or {}
        return Completion(
            text="".join(p.get("text", "") for p in parts),
            input_tokens=int(usage.get("promptTokenCount", 0)),
            output_tokens=int(usage.get("candidatesTokenCount", 0)),
        )


register("google", GoogleAdapter)
