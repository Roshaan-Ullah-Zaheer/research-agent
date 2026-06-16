"""The research agent pipeline.

``run_research`` orchestrates planning, searching, reading, writing, reviewing,
and a single optional revision round, yielding SSE-style event dicts as it goes.
The web layer is responsible for serializing these dicts to the client.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from . import llm, search
from .schemas import Plan, Review

logger = logging.getLogger(__name__)

# Tunable limits for the pipeline.
MAX_SOURCES = 10          # cap on sources kept during the initial search phase
MAX_SOURCES_AFTER_REVISION = 12  # cap after the revision round adds more
SNIPPET_CHARS = 200       # snippet length emitted in "source" events
CONTEXT_CHARS = 1500      # per-source content length in the writer context
SEARCH_RESULTS_PER_QUERY = 5
MAX_GAP_QUERIES = 2

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PLANNER_SYSTEM = (
    "You are a meticulous research strategist. Given a topic, you decompose it "
    "into a small set of focused, non-overlapping web-search sub-questions that "
    "together give comprehensive coverage. Each sub-question must be specific, "
    "self-contained, and phrased as an effective search query. Respond ONLY with "
    'a JSON object of the form {"questions": ["...", "..."]} containing between 3 '
    "and 5 questions. Do not include any text outside the JSON object."
)

WRITER_SYSTEM = (
    "You are a rigorous research writer. You write clear, well-structured "
    "Markdown reports grounded STRICTLY in the numbered sources provided to you. "
    "You must not introduce any fact, statistic, name, or claim that is not "
    "supported by the supplied sources. Cite every supported claim inline using "
    "bracketed numeric citations like [1] or [2][3], using ONLY the source ids "
    "given. Never invent or cite a source id that was not provided. If the "
    "sources do not cover an aspect of the topic, say so plainly rather than "
    "guessing."
)

REVIEWER_SYSTEM = (
    "You are a critical research editor. You evaluate a draft Markdown report "
    "against the sources it was written from. You judge coverage of the original "
    "topic, flag unsupported or weakly-supported claims, and identify missing "
    "angles. You decide whether more research is warranted. Respond ONLY with a "
    'JSON object of the form {"summary": "...", "needs_more": true|false, '
    '"gaps": ["search query 1", "search query 2"]}. The "gaps" array holds at '
    "most 2 additional web-search queries that would best fill the gaps; leave it "
    "empty if no further research is needed. Do not include any text outside the "
    "JSON object."
)


def _build_context(sources: dict[str, dict]) -> str:
    """Build a numbered context block from kept sources for the writer prompt."""
    blocks: list[str] = []
    for source in sources.values():
        content = (source.get("content") or "").strip()
        if len(content) > CONTEXT_CHARS:
            content = content[:CONTEXT_CHARS].rstrip() + "..."
        blocks.append(
            f"[{source['id']}] {source['title']}\n{source['url']}\n{content}"
        )
    return "\n\n".join(blocks)


def _writer_prompt(topic: str, context: str) -> str:
    """Compose the user prompt instructing the writer to produce the report."""
    return (
        f"Research topic: {topic}\n\n"
        "Write a polished Markdown research report answering the topic using ONLY "
        "the numbered sources below. Requirements:\n"
        "- Begin with a single `#` title.\n"
        "- Follow with a 1-2 sentence introduction.\n"
        "- Include 2-4 `##` sections covering the key findings.\n"
        "- End with a short conclusion.\n"
        "- Cite sources inline as [n] using ONLY the ids shown below. Every "
        "non-trivial claim must carry a citation.\n"
        "- Do NOT use any information beyond these sources, and do NOT add a "
        "references/sources list (the application renders that separately).\n\n"
        f"SOURCES:\n{context}"
    )


async def run_research(topic: str) -> AsyncGenerator[dict, None]:
    """Run the end-to-end research pipeline for ``topic``.

    Yields event dicts conforming to the SSE event protocol. Any unexpected
    exception is reported as ``status``/``error`` events instead of propagating.
    """
    try:
        # --- 1. Planning -------------------------------------------------
        yield {
            "type": "status",
            "phase": "planning",
            "message": "Breaking the topic into research questions",
        }
        plan: Plan = await llm.generate_json(
            system=PLANNER_SYSTEM,
            prompt=(
                f"Topic to research: {topic}\n\n"
                "Produce 3-5 focused web-search sub-questions covering the most "
                "important angles of this topic."
            ),
            schema=Plan,
        )  # type: ignore[assignment]
        questions = [q.strip() for q in plan.questions if q and q.strip()]
        yield {"type": "plan", "questions": questions}

        # --- 2. Searching ------------------------------------------------
        yield {
            "type": "status",
            "phase": "searching",
            "message": "Searching the web for relevant sources",
        }
        # Sources keyed by url to dedupe; each value carries an incremental id.
        sources: dict[str, dict] = {}
        next_id = 1

        for question in questions:
            results = await search.search(question, max_results=SEARCH_RESULTS_PER_QUERY)
            yield {
                "type": "search",
                "query": question,
                "results": [
                    {"title": r["title"], "url": r["url"]} for r in results
                ],
            }
            for result in results:
                if len(sources) >= MAX_SOURCES:
                    break
                url = result["url"]
                if url in sources:
                    continue
                source = {
                    "id": next_id,
                    "title": result["title"],
                    "url": url,
                    "content": result.get("content") or "",
                }
                sources[url] = source
                next_id += 1
                snippet = (source["content"] or "").strip()[:SNIPPET_CHARS]
                yield {
                    "type": "source",
                    "id": source["id"],
                    "title": source["title"],
                    "url": source["url"],
                    "snippet": snippet,
                }

        # --- 3. Reading --------------------------------------------------
        yield {
            "type": "status",
            "phase": "reading",
            "message": "Reading and compiling sources",
        }
        context = _build_context(sources)

        # --- 4. Writing --------------------------------------------------
        yield {
            "type": "status",
            "phase": "writing",
            "message": "Writing the research report",
        }
        report_md = await llm.generate_text(
            system=WRITER_SYSTEM,
            prompt=_writer_prompt(topic, context),
            temperature=0.4,
            max_tokens=2000,
        )
        yield {"type": "draft", "markdown": report_md}

        # --- 5. Reviewing ------------------------------------------------
        yield {
            "type": "status",
            "phase": "reviewing",
            "message": "Reviewing the draft for gaps",
        }
        review: Review = await llm.generate_json(
            system=REVIEWER_SYSTEM,
            prompt=(
                f"Original research topic: {topic}\n\n"
                "Critically review the draft report below. Assess how well it "
                "covers the topic, flag any claims that the sources do not "
                "support, and list missing angles. Decide whether another round "
                "of searching is needed, and if so provide up to 2 additional "
                "search queries.\n\n"
                f"SOURCES USED:\n{context}\n\n"
                f"DRAFT REPORT:\n{report_md}"
            ),
            schema=Review,
        )  # type: ignore[assignment]
        gaps = [g.strip() for g in review.gaps if g and g.strip()][:MAX_GAP_QUERIES]
        yield {
            "type": "review",
            "summary": review.summary,
            "needs_more": review.needs_more,
            "gaps": gaps,
        }

        # --- 6. Revising (single optional round) -------------------------
        if review.needs_more and gaps:
            yield {
                "type": "status",
                "phase": "revising",
                "message": "Filling gaps with additional research",
            }
            for gap_query in gaps:
                results = await search.search(
                    gap_query, max_results=SEARCH_RESULTS_PER_QUERY
                )
                yield {
                    "type": "search",
                    "query": gap_query,
                    "results": [
                        {"title": r["title"], "url": r["url"]} for r in results
                    ],
                }
                for result in results:
                    if len(sources) >= MAX_SOURCES_AFTER_REVISION:
                        break
                    url = result["url"]
                    if url in sources:
                        continue
                    source = {
                        "id": next_id,
                        "title": result["title"],
                        "url": url,
                        "content": result.get("content") or "",
                    }
                    sources[url] = source
                    next_id += 1
                    snippet = (source["content"] or "").strip()[:SNIPPET_CHARS]
                    yield {
                        "type": "source",
                        "id": source["id"],
                        "title": source["title"],
                        "url": source["url"],
                        "snippet": snippet,
                    }

            # Rebuild context with the expanded source set and rewrite.
            context = _build_context(sources)
            yield {
                "type": "status",
                "phase": "writing",
                "message": "Rewriting the report with new sources",
            }
            report_md = await llm.generate_text(
                system=WRITER_SYSTEM,
                prompt=_writer_prompt(topic, context),
                temperature=0.4,
                max_tokens=2000,
            )
            yield {"type": "draft", "markdown": report_md}

        # --- 7. Final report ---------------------------------------------
        yield {
            "type": "report",
            "markdown": report_md,
            "sources": [
                {"id": s["id"], "title": s["title"], "url": s["url"]}
                for s in sources.values()
            ],
        }
        yield {"type": "status", "phase": "done", "message": "Research complete"}
        yield {"type": "done"}

    except Exception as e:  # noqa: BLE001 - surface as protocol error events.
        logger.exception("[agent] research failed for topic %r", topic)
        yield {"type": "status", "phase": "error", "message": str(e)}
        yield {"type": "error", "message": str(e)}
