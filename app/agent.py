"""The research agent pipeline.

``run_research`` orchestrates planning, searching, reading, writing, reviewing,
and a single optional revision round, yielding SSE-style event dicts as it goes.
The web layer is responsible for serializing these dicts to the client.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import AsyncGenerator

from . import llm, search
from .schemas import Plan, Review

logger = logging.getLogger(__name__)


def _today() -> str:
    """Human-readable current date, injected so the agent can reason about
    'latest' / time-bound questions and form date-qualified search queries."""
    return date.today().strftime("%A, %d %B %Y")

# Tunable limits for the pipeline.
MAX_SOURCES = 10          # cap on sources kept during the initial search phase
MAX_SOURCES_AFTER_REVISION = 12  # cap after the revision round adds more
SNIPPET_CHARS = 200       # snippet length emitted in "source" events
CONTEXT_CHARS = 2000      # per-source content length in the writer context
SEARCH_RESULTS_PER_QUERY = 5
MAX_GAP_QUERIES = 2

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PLANNER_SYSTEM = (
    "# Role\n"
    "You are an expert research strategist who plans how to investigate a user's "
    "question on the open web.\n\n"
    "# Task\n"
    "Decompose the user's question into 3-5 focused, non-overlapping web-search "
    "sub-questions that together gather everything needed to answer the ORIGINAL "
    "question completely and specifically.\n\n"
    "# Instructions\n"
    "1. First decide what the user actually wants and the answer shape it demands:\n"
    "   - SPECIFIC DATA (numbers, prices, dates, statistics, rankings, a named "
    "value, or 'latest'/'current'/'past N days/weeks') -> your sub-questions MUST "
    "hunt for those exact figures, carrying the concrete entity, units, and time "
    "frame from the question (e.g. the specific index/metric and the dates). Do "
    "NOT settle for generic 'what is X' background when the user wants data.\n"
    "   - COMPARISON -> cover each option plus the criteria being compared.\n"
    "   - HOW-TO / STEPS -> cover prerequisites, the procedure, and pitfalls.\n"
    "   - OPEN / EXPLANATORY -> cover definition, key aspects, evidence, current state.\n"
    "2. Make every sub-question specific, self-contained, and phrased as an "
    "effective search-engine query (include real entities, units, and dates).\n"
    "3. Favor queries likely to surface authoritative, up-to-date primary sources.\n\n"
    "# Constraints\n"
    'Output ONLY a JSON object of the form {"questions": ["...", "..."]} with 3-5 '
    "items. No prose, no markdown, nothing outside the JSON object."
)

WRITER_SYSTEM = (
    "# Role\n"
    "You are a knowledgeable research analyst and writer. You produce clear, "
    "thorough, well-explained answers grounded strictly in numbered web sources.\n\n"
    "# Core principle\n"
    "Lead with a direct answer to the EXACT question asked, then EXPLAIN it "
    "properly. The reader wants a satisfying, informative answer they can learn "
    "from - never a throwaway one-liner. Be comprehensive and substantive while "
    "staying on-topic and grounded.\n\n"
    "# Instructions\n"
    "1. Open with the direct answer / key takeaway in the first sentence or two. "
    "If specific data was requested (numbers, dates, prices, a list), present it "
    "up front - a Markdown table when it has rows/columns (e.g. Date | Value) - "
    "using the real figures from the sources.\n"
    "2. THEN develop a full explanation: the important context, the how and why, "
    "key factors, comparisons, nuances, and implications the sources support. "
    "Organize anything with multiple parts using `##` sections and bullet or "
    "numbered lists, and give a longer report a short `#` title.\n"
    "3. Cover the relevant angles the sources allow - do not stop at the bare "
    "fact. Explain terms and reasoning so a non-expert comes away understanding "
    "the topic.\n"
    "4. Cite every non-trivial claim inline with [n] using ONLY the provided "
    "source ids (combine like [2][5] when needed). Write naturally - no filler, "
    "no restating the question, no 'as an AI' - but DO be thorough.\n\n"
    "# Length & depth\n"
    "Err on the side of MORE explanation. A typical answer is several "
    "well-developed paragraphs (roughly 250-500+ words). Only a pure factual "
    "lookup may be shorter, and even then add a couple of sentences of useful "
    "context. Never answer in just one or two lines when the sources support more.\n\n"
    "# Grounding constraints (critical)\n"
    "- Use ONLY the numbered sources. Never introduce a fact, number, name, or "
    "date they do not support, and never invent or cite an id that was not "
    "provided.\n"
    "- If the sources do NOT contain the specific thing asked for, say so plainly, "
    "then give the closest supported information and explain what is available. "
    "NEVER fabricate or estimate numbers to fill the gap.\n"
    "- Do not add a Sources/References list; the application renders it separately."
)

REVIEWER_SYSTEM = (
    "# Role\n"
    "You are a demanding research editor. You judge whether a draft truly answers "
    "the user's original question, grounded in its sources.\n\n"
    "# Task\n"
    "Review the draft and decide whether one more short round of web research is "
    "warranted.\n\n"
    "# Evaluation criteria (priority order)\n"
    "1. DIRECTNESS - Does the draft directly answer the EXACT question, with the "
    "specific information requested (the actual values/dates/list) up front? If the "
    "user wanted data and the draft gives background instead, that is the most "
    "important gap.\n"
    "2. GROUNDING - Are all claims supported by the cited sources? Flag anything "
    "unsupported or any citation that does not match.\n"
    "3. COVERAGE & DEPTH - Are important angles or parts of the question "
    "missing, and is the answer thorough and well-explained rather than a thin "
    "one or two lines? A draft too short or shallow for what the sources support "
    "is a gap.\n"
    "4. RECENCY/SPECIFICITY - For time-bound or 'latest' questions, is the answer "
    "current and specific enough?\n\n"
    "# Decide\n"
    "- If gaps remain AND more searching could plausibly fill them, set "
    "needs_more=true and give up to 2 targeted queries that would retrieve the "
    "MISSING specifics (include exact entities and time frames).\n"
    "- If the draft is already complete, direct, and grounded - or the missing data "
    "simply isn't available on the web - set needs_more=false and gaps=[].\n\n"
    "# Constraints\n"
    'Output ONLY a JSON object of the form {"summary": "...", "needs_more": '
    'true|false, "gaps": ["query 1", "query 2"]}. No text outside the JSON object.'
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
    """Compose the user prompt instructing the writer to produce the answer."""
    return (
        f"Today's date: {_today()}\n\n"
        f"The user asked: {topic}\n\n"
        "Write a thorough, well-explained answer to this exact question using "
        "ONLY the numbered sources below, following your system instructions:\n"
        "- Lead with the direct answer. If specific data was requested, put it "
        "first (a Markdown table for tabular data, otherwise a clear list), with "
        "the real figures drawn from the sources.\n"
        "- Then explain it properly: the context, the how and why, key factors, "
        "comparisons and implications the sources support. Aim for a genuinely "
        "informative answer (usually several paragraphs), organized with `##` "
        "sections and lists when there are multiple parts, and a short `#` title "
        "for a longer report.\n"
        "- Be substantive and explanatory - do NOT answer in just one or two "
        "lines. Explain terms so a non-expert understands.\n"
        "- Cite inline as [n] using ONLY the ids shown. If the sources lack the "
        "specific thing asked for, say so and give the closest supported info "
        "rather than guessing.\n"
        "- Do NOT add a references/sources list (the app renders that separately).\n\n"
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
                f"Today's date: {_today()}\n\n"
                f"The user's question: {topic}\n\n"
                "Produce 3-5 focused web-search sub-questions that will gather "
                "everything needed to answer THIS exact question. If it asks for "
                "specific data or recent figures, target those exact values and "
                "time frames rather than generic background."
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
                f"The user's original question: {topic}\n\n"
                "Critically review the draft below. Most importantly, judge "
                "whether it DIRECTLY answers this exact question with the specific "
                "information requested up front (not just background). Then check "
                "grounding against the sources, coverage, and recency. Decide "
                "whether another short round of searching is needed, and if so "
                "give up to 2 targeted queries for the missing specifics.\n\n"
                f"SOURCES USED:\n{context}\n\n"
                f"DRAFT:\n{report_md}"
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
