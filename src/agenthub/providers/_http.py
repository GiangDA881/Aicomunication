from __future__ import annotations

from typing import Any

import httpx

from agenthub.errors import ProviderError


async def post_json(
    url: str, headers: dict[str, str], body: dict[str, Any], timeout: float
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=body)
    except httpx.TimeoutException as exc:
        raise TimeoutError(f"provider timed out after {timeout}s") from exc
    except httpx.HTTPError as exc:
        raise ProviderError(f"provider request failed: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        # Body may echo request data; keep only a short, non-secret summary.
        raise ProviderError(f"provider returned HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()
