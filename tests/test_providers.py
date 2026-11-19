"""Adapters build the documented request shape and parse usage. No network: post_json is stubbed."""

from __future__ import annotations

import pytest

from agenthub.providers import Message, get_adapter


@pytest.mark.parametrize("name,response,expect_url", [
    ("openai", {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}},
     "https://api.openai.com/v1/chat/completions"),
    ("anthropic", {"content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 3, "output_tokens": 1}},
     "https://api.anthropic.com/v1/messages"),
    ("google", {"candidates": [{"content": {"parts": [{"text": "hi"}]}}],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1}},
     "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent"),
])
async def test_adapter_shapes(monkeypatch, name, response, expect_url):
    seen = {}

    async def fake(url, headers, body, timeout):
        seen.update(url=url, headers=headers, body=body)
        return response

    mod = __import__(f"agenthub.providers.{name}", fromlist=["x"])
    monkeypatch.setattr(mod, "post_json", fake)
    c = await get_adapter(name).complete(model="m", system="sys", messages=[Message("user", "q")],
                                         api_key="KEY", base_url=None, params={"max_tokens": 10}, timeout=5)
    assert (c.text, c.input_tokens, c.output_tokens) == ("hi", 3, 1)
    assert seen["url"] == expect_url
    assert "KEY" in str(seen["headers"]) and "KEY" not in str(seen["body"])
