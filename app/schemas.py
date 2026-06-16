"""Pydantic v2 models for structured LLM output.

These models describe the JSON the LLM must return for the planning and review
stages. SSE events emitted by the agent are plain dicts and intentionally have
no model here.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Plan(BaseModel):
    """The research plan: focused web-search sub-questions for a topic."""

    questions: list[str] = Field(
        ...,
        description="3-5 focused web-search sub-questions for the topic.",
    )


class Review(BaseModel):
    """Critique of a draft report produced during the review stage."""

    summary: str = Field(
        ...,
        description="Short critique of the draft's coverage and quality.",
    )
    needs_more: bool = Field(
        ...,
        description="True if additional searching is needed to fill gaps.",
    )
    gaps: list[str] = Field(
        default_factory=list,
        description="Up to 2 additional search queries to fill missing angles.",
    )
