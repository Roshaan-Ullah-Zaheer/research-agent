"""Application configuration.

Loads environment variables from ``.env.local`` in the project root (falling
back to the default ``.env`` discovery) and exposes the API credentials used by
the research agent's tech stack: Gemini (Google), Groq, and Tavily.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Project root is the parent of the ``app`` package directory.
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
ENV_LOCAL_PATH: Path = PROJECT_ROOT / ".env.local"

# Prefer an explicit ``.env.local`` in the project root; then fall back to the
# default load_dotenv() behavior (searches for a plain ``.env``) without
# overriding anything already set above.
load_dotenv(dotenv_path=ENV_LOCAL_PATH)
load_dotenv()


def _parse_keys(raw: str | None) -> list[str]:
    """Split a comma-separated env value into a clean list of non-empty keys."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


# Comma-separated list of Gemini API keys; rotated through for fallback.
GOOGLE_API_KEYS: list[str] = _parse_keys(os.getenv("GOOGLE_API_KEYS"))

# Single Groq key (text generation fallback after all Gemini candidates).
GROQ_API_KEY: str | None = os.getenv("GROQ_API_KEY") or None

# Single Tavily key (web search).
TAVILY_API_KEY: str | None = os.getenv("TAVILY_API_KEY") or None


def missing_keys() -> list[str]:
    """Return the names of required credentials that are not configured.

    Text generation requires at least one Gemini key *or* the Groq key, so it is
    reported as missing only when both are absent. Tavily is always required for
    web search.
    """
    missing: list[str] = []
    if not GOOGLE_API_KEYS and not GROQ_API_KEY:
        # Neither text provider is configured.
        missing.append("GOOGLE_API_KEYS_or_GROQ_API_KEY")
    if not TAVILY_API_KEY:
        missing.append("TAVILY_API_KEY")
    return missing
