import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from langsmith import Client, tracing_context
from swarm import observability
from swarm.tool_registry import ToolRegistry, ToolError


def test_nexus_defaults_and_disabled_trace(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    assert observability.project() == "swarm-nexus-production"
    assert observability.health()["configured"] is False
    with observability.assignment_trace({"id": "fixture"}):
        from langsmith import get_tracing_context
        assert get_tracing_context()["enabled"] is False


def test_trace_identity_and_body_exception_are_preserved(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key-not-real")
    monkeypatch.setenv("NEXUS_RELEASE", "test-release")
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    captured = []
    @contextmanager
    def capture(**kwargs):
        captured.append(kwargs)
        yield
    monkeypatch.setattr("langsmith.tracing_context", capture)
    with pytest.raises(ImportError, match="assignment failure"):
        with observability.assignment_trace({"task_id":"task", "id":"item", "role":"worker", "attempt":2}):
            raise ImportError("assignment failure")
    assert len(captured) == 1
    assert captured[0]["enabled"] is True
    assert captured[0]["tags"] == ["swarm-nexus", "worker", "durable-assignment"]
    assert captured[0]["metadata"] == {
        "task_id":"task", "item_id":"item", "attempt":2, "model":"deepseek-v4-flash",
        "service":"swarm-nexus", "release":"test-release",
    }


@pytest.mark.asyncio
async def test_shutdown_flush_uses_cached_client_and_sdk_timeout(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key-not-real")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    loop_thread = __import__("threading").get_ident()
    calls = []
    def flush(*, timeout):
        calls.append((timeout, __import__("threading").get_ident()))
    client = SimpleNamespace(flush=flush)
    monkeypatch.setattr("langsmith.run_trees.get_cached_client", lambda: client)
    assert await observability.flush_traces(timeout=0.25) is True
    assert calls[0][0] == 0.25
    assert calls[0][1] != loop_thread


@pytest.mark.asyncio
async def test_flush_disabled_never_constructs_client(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    def forbidden():
        raise AssertionError("No tracing client should be created")
    monkeypatch.setattr("langsmith.run_trees.get_cached_client", forbidden)
    assert await observability.flush_traces() is False


@pytest.mark.asyncio
async def test_flush_failure_is_nonfatal_and_sanitized(monkeypatch, caplog):
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key-not-real")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    def fail(*, timeout):
        raise RuntimeError("DO_NOT_LOG_SECRET")
    monkeypatch.setattr("langsmith.run_trees.get_cached_client", lambda: SimpleNamespace(flush=fail))
    assert await observability.flush_traces() is False
    assert "RuntimeError" in caplog.text
    assert "DO_NOT_LOG_SECRET" not in caplog.text


@pytest.mark.asyncio
async def test_tools_emit_spans_without_registry_context_or_credentials(monkeypatch):
    created, updated = [], []
    # Capture SDK calls in memory. No HTTP transport is invoked.
    monkeypatch.setattr(Client, "create_run", lambda self, *args, **kwargs: created.append(kwargs))
    monkeypatch.setattr(Client, "update_run", lambda self, *args, **kwargs: updated.append(kwargs))
    client = Client(api_url="https://tracing.example.test", api_key="fixture-not-a-key", auto_batch_tracing=False)
    registry = ToolRegistry({"private_context": "DO_NOT_SERIALIZE_REGISTRY"})
    with tracing_context(enabled=True, client=client, project_name="fixture"):
        assert (await registry.call("calculate", {"expression":"13*17"}))["result"] == 221
        with pytest.raises(ToolError):
            await registry.call("unknown", {})
    assert len(created) == 2
    assert all(run["run_type"] == "tool" for run in created)
    assert created[0]["inputs"] == {"name":"calculate", "arguments":{"expression":"13*17"}}
    assert "DO_NOT_SERIALIZE_REGISTRY" not in str(created)
    assert len(updated) == 2
    assert any(run.get("error") for run in updated)


def test_api_lifespan_flushes_after_engine_shutdown(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from fastapi.testclient import TestClient
    from swarm.api import Settings, create_app
    flush = AsyncMock(return_value=True)
    monkeypatch.setattr("swarm.api.flush_traces", flush)
    app = create_app(Settings(db_path=str(tmp_path / "swarm.db")),
                     provider=SimpleNamespace(configured=False), start_engine=False)
    with TestClient(app):
        flush.assert_not_awaited()
    flush.assert_awaited_once_with()
