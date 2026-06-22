"""FastAPI application for the Research Agent.

Exposes a Server-Sent Events (SSE) endpoint that streams the research agent's
progress to the client, plus a small static frontend and a health check.
"""

import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from app.agent import run_research

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("research_agent.api")

# Resolve the static directory from this file's location so it works regardless
# of the current working directory (e.g. when started by uvicorn in the container).
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(
    title="Research Agent",
    description="An AI research agent that plans, searches the web, drafts, "
    "and reviews a report — streamed live over Server-Sent Events.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    """Lightweight liveness probe."""
    return {"status": "ok"}


@app.get("/api/research")
async def research(request: Request, topic: str = ""):
    """Stream the research agent's progress for ``topic`` as SSE.

    Each event yielded by ``run_research`` is serialized to JSON and sent in the
    SSE ``data`` field. The stream stops early if the client disconnects, and any
    unexpected error is surfaced to the client as a final ``error`` event.
    """
    topic = (topic or "").strip()
    if not topic:
        return JSONResponse(
            status_code=400,
            content={"type": "error", "message": "Query parameter 'topic' is required."},
        )

    async def event_generator():
        logger.info("Starting research stream for topic: %r", topic)
        try:
            async for event in run_research(topic):
                if await request.is_disconnected():
                    logger.info("Client disconnected; stopping stream.")
                    break
                yield {"data": json.dumps(event)}
        except Exception as exc:  # noqa: BLE001 - surface any failure to the client
            logger.exception("Research stream failed for topic %r", topic)
            yield {"data": json.dumps({"type": "error", "message": str(exc)})}
        finally:
            logger.info("Research stream finished for topic: %r", topic)

    return EventSourceResponse(event_generator())


@app.get("/")
async def index() -> FileResponse:
    """Serve the single-page frontend."""
    return FileResponse(STATIC_DIR / "index.html")


# Mount the static assets last so it never shadows the API/health/root routes
# above. The frontend references assets via "/static/app.js" and
# "/static/styles.css".
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
