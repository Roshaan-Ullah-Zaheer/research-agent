"""Web search via the Tavily API."""

from __future__ import annotations

import logging

import httpx

from . import config

logger = logging.getLogger(__name__)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
SEARCH_TIMEOUT = 30.0


async def search(query: str, max_results: int = 5) -> list[dict]:
    """Search the web for ``query`` and return normalized result dicts.

    Each result is ``{"title": str, "url": str, "content": str, "score": float}``.
    On any error (missing key, network failure, bad response) this logs and
    returns an empty list rather than raising.
    """
    if not config.TAVILY_API_KEY:
        logger.error("[search] TAVILY_API_KEY is not configured; returning no results")
        return []

    payload = {
        "api_key": config.TAVILY_API_KEY,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
    }

    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
            response = await client.post(TAVILY_SEARCH_URL, json=payload)
            response.raise_for_status()
            data = response.json()
    except Exception as err:  # noqa: BLE001 - any failure degrades to empty results.
        logger.error("[search] query %r failed: %s", query, err)
        return []

    raw_results = data.get("results") or []
    normalized: list[dict] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not url:
            continue
        normalized.append(
            {
                "title": item.get("title") or url,
                "url": url,
                "content": item.get("content") or "",
                "score": float(item.get("score") or 0.0),
            }
        )
    return normalized
