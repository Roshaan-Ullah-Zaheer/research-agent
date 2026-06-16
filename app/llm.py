"""LLM access layer with multi-provider fallback.

A single ``openai.AsyncOpenAI`` client targets every provider via their
OpenAI-compatible endpoints. Candidates are tried in order; on failure the next
candidate is attempted. The chain is:

    for each Gemini key:
        (key, "gemini-2.5-flash")
        (key, "gemini-2.5-flash-lite")
    then (Groq key, "llama-3.3-70b-versatile")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from . import config

logger = logging.getLogger(__name__)

# OpenAI-compatible endpoints for each provider.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Gemini model preference order (per key).
GEMINI_MODELS = ("gemini-2.5-flash", "gemini-2.5-flash-lite")
GROQ_MODEL = "llama-3.3-70b-versatile"

# Per-request timeout in seconds.
REQUEST_TIMEOUT = 60.0


@dataclass(frozen=True)
class _Candidate:
    """A single (provider, model, base_url, api_key) attempt target."""

    provider: str
    model: str
    base_url: str
    api_key: str


def _build_candidates() -> list[_Candidate]:
    """Build the ordered fallback chain from currently configured keys.

    Built lazily (per call) so updated env/config is always reflected.
    """
    candidates: list[_Candidate] = []
    for key in config.GOOGLE_API_KEYS:
        for model in GEMINI_MODELS:
            candidates.append(
                _Candidate(
                    provider="gemini",
                    model=model,
                    base_url=GEMINI_BASE_URL,
                    api_key=key,
                )
            )
    if config.GROQ_API_KEY:
        candidates.append(
            _Candidate(
                provider="groq",
                model=GROQ_MODEL,
                base_url=GROQ_BASE_URL,
                api_key=config.GROQ_API_KEY,
            )
        )
    return candidates


def _client_for(candidate: _Candidate) -> AsyncOpenAI:
    """Create a fresh AsyncOpenAI client targeting the candidate's endpoint."""
    return AsyncOpenAI(
        base_url=candidate.base_url,
        api_key=candidate.api_key,
        timeout=REQUEST_TIMEOUT,
    )


async def generate_text(
    system: str,
    prompt: str,
    temperature: float = 0.4,
    max_tokens: int = 2000,
) -> str:
    """Generate free-form text, running the provider fallback chain.

    Returns the assistant message content string from the first candidate that
    succeeds. Raises the last exception if every candidate fails.
    """
    candidates = _build_candidates()
    if not candidates:
        raise RuntimeError("No LLM providers configured (set GOOGLE_API_KEYS or GROQ_API_KEY).")

    last_error: Exception | None = None
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]

    for candidate in candidates:
        client = _client_for(candidate)
        try:
            response = await client.chat.completions.create(
                model=candidate.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = response.choices[0].message.content
            if content is None or not content.strip():
                raise ValueError("empty completion content")
            return content
        except Exception as err:  # noqa: BLE001 - log and fall through to next candidate.
            last_error = err
            logger.warning("[llm] %s/%s failed: %s", candidate.provider, candidate.model, err)
            continue

    assert last_error is not None  # candidates was non-empty, so a failure exists.
    raise last_error


def _extract_json(text: str) -> dict:
    """Best-effort parse of a JSON object from raw model output.

    Handles responses wrapped in markdown code fences or with surrounding prose
    by falling back to the outermost ``{...}`` span.
    """
    text = text.strip()
    # Strip a leading/trailing markdown code fence if present.
    if text.startswith("```"):
        # Remove the first fence line (``` or ```json) and the trailing fence.
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -3]
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the outermost brace span.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


async def generate_json(
    system: str,
    prompt: str,
    schema: type[BaseModel],
) -> BaseModel:
    """Generate JSON validated against ``schema``, running the fallback chain.

    Requests ``response_format={"type": "json_object"}``. If a candidate returns
    unparseable or schema-invalid JSON, logs and tries the next candidate.
    Returns the validated model instance. Raises the last error if all fail.
    """
    candidates = _build_candidates()
    if not candidates:
        raise RuntimeError("No LLM providers configured (set GOOGLE_API_KEYS or GROQ_API_KEY).")

    last_error: Exception | None = None
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]

    for candidate in candidates:
        client = _client_for(candidate)
        try:
            response = await client.chat.completions.create(
                model=candidate.model,
                messages=messages,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            if content is None or not content.strip():
                raise ValueError("empty completion content")
            data = _extract_json(content)
            return schema.model_validate(data)
        except (ValueError, json.JSONDecodeError, ValidationError) as err:
            # Parse/validation problems: try the next candidate.
            last_error = err
            logger.warning(
                "[llm] %s/%s failed: invalid JSON (%s)",
                candidate.provider,
                candidate.model,
                err,
            )
            continue
        except Exception as err:  # noqa: BLE001 - network/API errors; fall through.
            last_error = err
            logger.warning("[llm] %s/%s failed: %s", candidate.provider, candidate.model, err)
            continue

    assert last_error is not None
    raise last_error
