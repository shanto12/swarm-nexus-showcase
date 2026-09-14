"""Regression coverage for paid retries admitted before sibling settlement."""
import asyncio
import json

import pytest

from swarm.engine import Engine
from swarm.provider import ProviderError
from swarm.store import Store


def answer(tokens=100, content="Useful saved evidence", **extra):
    return {"content": content, "usage": {"total_tokens": tokens}, **extra}


def two_workers(store, attempts=2):
    task = store.create_task("owner", "Produce two useful findings", 2, attempts, 100000)
    planner = store.claim("test")
    result = answer()
    store.checkpoint_response(planner, result)
    store.complete(planner, result, plan=[
        {"id": "a", "title": "Finding A", "prompt": "Produce A", "dependencies": []},
        {"id": "b", "title": "Finding B", "prompt": "Produce B", "dependencies": []},
    ])
    return task, store.claim("test"), store.claim("test")


def make_ready(store, item):
    # Advance scheduler time deterministically without a real two-second sleep.
    with store.tx() as db:
        db.execute("UPDATE items SET available_at=0 WHERE id=?", (item["id"],))


def row(store, item):
    return dict(store.db.execute("SELECT * FROM items WHERE id=?", (item["id"],)).fetchone())


def test_paid_wait_survives_restart_and_retries_only_after_sibling_settlement(tmp_path):
    path = tmp_path / "settlement.db"
    store = Store(path)
    task, sibling, deferred = two_workers(store)
    try:
        store.checkpoint_response(deferred, answer(1000, ""))
        assert store.defer_budget_contention(deferred)
        make_ready(store, deferred)
        assert row(store, deferred)["attempt"] == 1
        assert store.claim("test") is None
        store.close()
        store = Store(path)
        assert store.claim("after-restart") is None
        assert store.get_task(task["id"])["tokens_used"] == 1100
        store.checkpoint_response(sibling, answer(200))
        assert store.complete(sibling, answer(200))
        retry = store.claim("after-restart")
        assert retry["id"] == deferred["id"] and retry["attempt"] == 2
        assert store.get_task(task["id"])["token_budget"] == 100000
        assert store.get_task(task["id"])["tokens_uncertain"] == 0
    finally:
        store.close()


def test_definitely_unpaid_wait_refunds_attempt_and_waits_for_settlement(tmp_path):
    store = Store(tmp_path / "unpaid.db")
    try:
        task, sibling, deferred = two_workers(store, attempts=1)
        store.checkpoint_response(deferred, answer(0, ""))
        assert store.defer_budget_contention(deferred)
        make_ready(store, deferred)
        assert row(store, deferred)["attempt"] == 0
        assert store.claim("test") is None
        store.checkpoint_response(sibling, answer())
        store.complete(sibling, answer())
        retry = store.claim("test")
        assert retry["id"] == deferred["id"] and retry["attempt"] == 1
        assert store.get_task(task["id"])["tokens_used"] == 200
    finally:
        store.close()


@pytest.mark.parametrize("receipt_kind", ["uncertain", "missing", "cost_only"])
def test_nonzero_or_unconfirmed_charge_never_refunds_attempt_or_releases_funds(tmp_path, receipt_kind):
    store = Store(tmp_path / (receipt_kind + ".db"))
    try:
        task, sibling, deferred = two_workers(store)
        reserved = deferred["reserved_tokens"]
        if receipt_kind == "uncertain":
            store.checkpoint_response(deferred, answer(0, "", usage_uncertain=True))
        elif receipt_kind == "cost_only":
            store.checkpoint_response(deferred, answer(0, "", estimated_cost_usd=0.01))
        assert store.defer_budget_contention(deferred)
        assert row(store, deferred)["attempt"] == 1
        current = store.get_task(task["id"])
        assert current["tokens_used"] == 100
        assert current["tokens_uncertain"] == (0 if receipt_kind == "cost_only" else reserved)
        if receipt_kind == "cost_only":
            assert current["estimated_cost_usd"] == 0.01
        make_ready(store, deferred)
        assert store.claim("test") is None
    finally:
        store.close()


def test_stale_lease_cannot_stop_task_after_new_attempt_starts(tmp_path):
    store = Store(tmp_path / "stale.db")
    try:
        task, sibling, old = two_workers(store)
        store.checkpoint_response(old, answer(1000, ""))
        assert store.defer_budget_contention(old)
        store.checkpoint_response(sibling, answer())
        store.complete(sibling, answer())
        make_ready(store, old)
        replacement = store.claim("new-engine")
        with store.tx() as db:
            store._insert_item(db, task["id"], "Later sibling", "worker", "Produce C")
        assert store.claim("new-engine") is not None
        before = store.detail(task["id"])
        assert not store.defer_budget_contention({**old, "attempt": 2})
        after = store.detail(task["id"])
        assert after == before
        assert row(store, replacement)["lease_token"] == replacement["lease_token"]
    finally:
        store.close()


def test_paid_attempt_ceiling_still_blocks_after_settlement_wait(tmp_path):
    store = Store(tmp_path / "ceiling.db")
    try:
        task, sibling, deferred = two_workers(store, attempts=1)
        store.checkpoint_response(deferred, answer(1000, ""))
        assert store.defer_budget_contention(deferred)
        current = store.get_task(task["id"])
        assert current["status"] == "blocked"
        assert current["tokens_used"] == 1100
        assert current["tokens_uncertain"] == sibling["reserved_tokens"]
        assert current["max_attempts"] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_engine_finishes_useful_work_without_paid_replay_while_sibling_runs(tmp_path):
    store = Store(tmp_path / "engine.db")
    task, sibling, deferred = two_workers(store)
    # Return the claimed fixtures to the queue as a test-only pre-execution setup.
    with store.tx() as db:
        db.execute("UPDATE items SET status='queued',attempt=0,lease_token=NULL,lease_until=NULL,reserved_tokens=0 WHERE id IN (?,?)", (sibling["id"], deferred["id"]))
    release_sibling = asyncio.Event()
    sibling_entered = asyncio.Event()
    calls = []

    class ContendingProvider:
        async def execute(self, role, prompt, context, **kwargs):
            calls.append((context["item_id"], context["attempt"]))
            if context["item_id"] == sibling["id"]:
                sibling_entered.set()
                await release_sibling.wait()
                return answer(200, "Reliability acceptance tests are saved.")
            if context["item_id"] == deferred["id"]:
                if context["attempt"] == 1:
                    await sibling_entered.wait()
                    raise ProviderError("Remaining task token budget cannot safely cover another model request.", usage={"total_tokens": 1000})
                assert release_sibling.is_set()
                return answer(300, "Security acceptance tests are saved.")
            assert role == "verifier"
            assert len(context["dependencies"]) == 2
            return answer(400, json.dumps({"accepted": True, "result": "Launch requires passing reliability and security acceptance tests.", "issues": []}))

    engine = Engine(store, ContendingProvider(), poll_seconds=0.01)
    try:
        async with asyncio.timeout(5):
            while not any(e["kind"] == "resource_wait" for e in store.detail(task["id"])["events"]):
                await engine.tick()
                await asyncio.sleep(0.01)
            make_ready(store, deferred)
            for _ in range(3):
                await engine.tick()
                await asyncio.sleep(0.01)
            assert [x for x in calls if x[0] == deferred["id"]] == [(deferred["id"], 1)]
            release_sibling.set()
            while store.get_task(task["id"])["status"] != "completed":
                await engine.tick()
                await asyncio.sleep(0.01)
        current = store.get_task(task["id"])
        assert current["tokens_used"] == 2000
        assert current["tokens_uncertain"] == 0
        assert current["token_budget"] == 100000 and current["max_attempts"] == 2
        assert "reliability and security" in current["result"]
        assert [x for x in calls if x[0] == deferred["id"]] == [(deferred["id"], 1), (deferred["id"], 2)]
    finally:
        release_sibling.set()
        await engine.close()
        store.close()
