"""Optional LangSmith tracing for durable swarm assignments.

Tracing is deliberately server-side. When a LangSmith key is absent the
runtime remains usable, but health reports observability as unconfigured and
assignment tracing is explicitly disabled even if a global flag is enabled.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import contextmanager

logger = logging.getLogger(__name__)


def configured() -> bool:
    return bool(os.environ.get("LANGSMITH_API_KEY", "").strip()) and os.environ.get(
        "LANGSMITH_TRACING", "true"
    ).lower() == "true"


def project() -> str:
    return os.environ.get("LANGSMITH_PROJECT", "swarm-nexus-production").strip() or "swarm-nexus-production"


def endpoint() -> str:
    return os.environ.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com").rstrip("/")


@contextmanager
def assignment_trace(item: dict):
    """Attach Nexus identity to the graph and its nested model/tool spans."""
    try:
        from langsmith import tracing_context
    except ImportError:
        yield
        return
    if not configured():
        with tracing_context(enabled=False):
            yield
        return
    # Do not catch exceptions raised by the assignment body. In particular,
    # catching ImportError around yield would incorrectly yield a second time.
    with tracing_context(
            enabled=True,
            project_name=project(),
            tags=["swarm-nexus", item.get("role", "agent"), "durable-assignment"],
            metadata={
                "service": "swarm-nexus",
                "release": os.environ.get("NEXUS_RELEASE", "development"),
                "task_id": item.get("task_id"),
                "item_id": item.get("id"),
                "attempt": item.get("attempt"),
                "model": "deepseek-v4-flash",
            },
        ):
        yield


async def flush_traces(timeout: float = 8.0) -> bool:
    """Drain the same SDK client off the event loop, with a bounded wait.

    A successful flush call is not proof of ingestion. Production verification
    must separately retrieve matching traces from the intended project.
    """
    if not configured():
        return False
    try:
        from langsmith.run_trees import get_cached_client

        client = get_cached_client()
        await asyncio.wait_for(
            asyncio.to_thread(client.flush, timeout=timeout), timeout=timeout + 1
        )
        return True
    except Exception as exc:
        # Exception strings may include request details; log the class only.
        logger.warning("LangSmith shutdown flush did not finish (%s)", type(exc).__name__)
        return False


def health() -> dict:
    return {
        "provider": "langsmith",
        "configured": configured(),
        "tracing_enabled": os.environ.get("LANGSMITH_TRACING", "true").lower() == "true",
        "project": project(),
        "endpoint": endpoint(),
    }
