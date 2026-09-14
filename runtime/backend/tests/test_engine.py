import asyncio
import json
import subprocess
import sys

import pytest

from swarm.engine import Engine, validate_plan, validate_verification
from swarm.store import Store


class EvidenceProvider:
    """Deterministic fault-injection fixture, never used by the application."""
    def __init__(self, reject_once=False, delay=0.005, execution_barrier=None):
        self.active = 0
        self.maximum = 0
        self.by_task = {}
        self.max_by_task = {}
        self.calls = []
        self.verifications = 0
        self.reject_once = reject_once
        self.delay = delay
        self.execution_barrier = execution_barrier

    async def execute(self, role, prompt, context, tools_enabled=True):
        tid = context["task_id"]
        self.active += 1
        self.by_task[tid] = self.by_task.get(tid, 0) + 1
        self.maximum = max(self.maximum, self.active)
        self.max_by_task[tid] = max(self.max_by_task.get(tid, 0), self.by_task[tid])
        self.calls.append((role, context))
        try:
            if self.execution_barrier is not None:
                await self.execution_barrier(role, context)
            await asyncio.sleep(self.delay)
            if role == "planner":
                content = json.dumps({"items": [
                    {"id": "a", "title": "Research", "prompt": "Research", "dependencies": []},
                    {"id": "b", "title": "Check", "prompt": "Check", "dependencies": []},
                    {"id": "c", "title": "Synthesize", "prompt": "Synthesize", "dependencies": ["a", "b"]},
                ]})
            elif role == "verifier":
                self.verifications += 1
                accepted = not self.reject_once or self.verifications > 1
                content = json.dumps({"accepted": accepted, "result": "Evidence-backed combined deliverable", "issues": [] if accepted else ["Missing an independently checked source"]})
            else:
                content = "Checked source evidence and dependency outputs: " + str(len(context["dependencies"]))
            return {"content": content, "usage": {"total_tokens": 100}, "estimated_cost_usd": 0.001,
                    "handoffs": [{"target_item_id": None, "message": "Source checks saved."}] if role == "worker" else []}
        finally:
            self.active -= 1
            self.by_task[tid] -= 1


async def drive(engine, tasks, timeout=10):
    async with asyncio.timeout(timeout):
        while True:
            await engine.tick()
            if all(engine.store.get_task(t)["status"] in {"completed", "blocked", "budget_exhausted", "cancelled", "failed"} for t in tasks):
                await engine.tick()
                return
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_budget_increase_resumes_verifier_without_replaying_paid_workers(tmp_path):
    from swarm.provider import ProviderError

    class BudgetProvider(EvidenceProvider):
        exhausted = False

        async def execute(self, role, prompt, context, tools_enabled=True):
            if role == "verifier" and not self.exhausted:
                self.exhausted = True
                raise ProviderError("Token budget cannot fund the next request.",
                                    usage={"total_tokens": 200}, usage_uncertain=True)
            return await super().execute(role, prompt, context, tools_enabled)

    store = Store(tmp_path / "budget.db")
    provider = BudgetProvider()
    engine = Engine(store, provider)
    task = store.create_task("owner", "Preserve paid research", 1, token_budget=1000)
    try:
        await drive(engine, [task["id"]])
        before = store.detail(task["id"])
        assert before["task"]["status"] == "budget_exhausted"
        assert before["task"]["tokens_used"] == 600
        assert before["task"]["tokens_uncertain"] == 400
        verifier = next(i for i in before["items"] if i["role"] == "verifier")
        assert verifier["status"] == "failed" and verifier["attempt"] == 1
        completed = [i for i in before["items"] if i["status"] == "completed"]
        artifacts = [store.get_artifact(task["id"], a["id"]) for a in before["artifacts"]]
        resumed = store.increase_budget(task["id"], 3000, "owner")
        assert resumed["status"] == "queued" and resumed["blocker"] is None
        for key in ("tokens_used", "tokens_uncertain", "estimated_cost_usd"):
            assert resumed[key] == before["task"][key]
        await drive(engine, [task["id"]])
        after = store.detail(task["id"])
        assert after["task"]["status"] == "completed"
        assert after["task"]["tokens_used"] == 700
        assert after["task"]["tokens_uncertain"] == 400
        assert [i for i in after["items"] if i["id"] != verifier["id"]] == completed
        assert next(i for i in after["items"] if i["id"] == verifier["id"])["attempt"] == 2
        assert [store.get_artifact(task["id"], a["id"]) for a in artifacts] == artifacts
        assert sum(role == "worker" for role, _ in provider.calls) == 3
        assert any(e["kind"] == "budget_increased" and e["agent"] == "owner" for e in after["events"])
    finally:
        await engine.close()
        store.close()


def test_budget_increase_preserves_paid_checkpoint_and_retry_limit(tmp_path):
    store = Store(tmp_path / "checkpoint-budget.db")
    try:
        task = store.create_task("owner", "Paid checkpoint", 1, max_attempts=1, token_budget=1000)
        item = store.claim("engine")
        response = {"content": "paid", "usage": {"total_tokens": 1000}}
        store.checkpoint_response(item, response)
        with store.tx() as db:
            store._stop(db, task["id"], "budget_exhausted", "Token budget reached")
        store.increase_budget(task["id"], 2000, "owner")
        claimed = store.claim("engine")
        assert claimed["id"] == item["id"] and claimed["attempt"] == 1
        assert json.loads(claimed["response"]) == response
        store.fail(claimed, "Token budget reached", recoverable=False, terminal_status="budget_exhausted")
        before = store.detail(task["id"])
        with pytest.raises(ValueError, match="retry limit"):
            store.increase_budget(task["id"], 3000, "owner")
        assert store.detail(task["id"]) == before
    finally:
        store.close()


@pytest.mark.asyncio
async def test_pool_fanout_dependencies_repair_and_global_limit(tmp_path):
    store = Store(tmp_path / "state.db")
    fanout_ready = asyncio.Event()
    fanout_entered = 0

    async def overlap_independent_workers(role, context):
        nonlocal fanout_entered
        if context["task_id"] == a["id"] and role == "worker" and not context["dependencies"]:
            fanout_entered += 1
            if fanout_entered == 2:
                fanout_ready.set()
            await fanout_ready.wait()

    provider = EvidenceProvider(reject_once=True, execution_barrier=overlap_independent_workers)
    engine = Engine(store, provider, global_limit=3)
    a = store.create_task("owner", "Research two sources and combine", 2)
    b = store.create_task("owner", "Other work", 1)
    try:
        await drive(engine, [a["id"], b["id"]])
        assert provider.maximum <= 3
        assert provider.max_by_task[a["id"]] == 2
        assert provider.max_by_task[b["id"]] == 1
        assert all(store.get_task(t["id"])["status"] == "completed" for t in [a, b])
        details = [store.detail(t["id"]) for t in [a, b]]
        assert any(e["kind"] == "repair" for d in details for e in d["events"])
        assert any(len(c["dependencies"]) == 2 and c["handoffs"] for role, c in provider.calls if role == "worker")
        assert any(c["handoffs"] for role, c in provider.calls if role == "worker")
        for detail in details:
            artifact = next(x for x in detail["artifacts"] if x["name"] == "result.md")
            assert "Evidence-backed" in store.get_artifact(detail["task"]["id"], artifact["id"], "owner")["content"]
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
async def test_restart_recovers_response_without_duplicate_publication(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    task = store.create_task("owner", "Research", 2)
    item = store.claim("crashed-process", lease_seconds=-1)
    provider = EvidenceProvider()
    response = await provider.execute("planner", "", store.context(item))
    store.checkpoint_response(item, response)
    store.close()  # crash after durable response, before plan publication
    store = Store(path)
    resumed = EvidenceProvider()
    engine = Engine(store, resumed)
    try:
        await drive(engine, [task["id"]])
        assert all(role != "planner" for role, _ in resumed.calls)
        detail = store.detail(task["id"])
        assert detail["task"]["status"] == "completed"
        assert len([x for x in detail["items"] if x["role"] == "worker"]) == 3
        assert detail["task"]["tokens_used"] == 500
        assert any(x["kind"] == "recovered" for x in detail["events"])
        assert (tmp_path / "state.db.checkpoints").exists()
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
async def test_worker_restart_mid_request_pause_resume_and_cancel_descendants(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    provider = EvidenceProvider(delay=0.1)
    engine = Engine(store, provider, lease_seconds=0.15)
    task = store.create_task("owner", "Persistent task", 2)
    await engine.tick()
    await asyncio.sleep(0.02)
    await engine.close()
    store.close()
    store = Store(path)
    engine = Engine(store, provider, lease_seconds=0.15)
    try:
        store.control(task["id"], "pause", "owner")
        await engine.tick()
        assert not engine.running
        store.steer(task["id"], "Include source checks", "owner")
        store.control(task["id"], "resume", "owner")
        async with asyncio.timeout(3):
            while not any(i["role"] == "worker" and i["status"] == "running" for i in store.detail(task["id"])["items"]):
                await engine.tick()
                await asyncio.sleep(0.005)
        store.control(task["id"], "cancel", "owner")
        await engine.tick()
        await asyncio.sleep(0.02)
        await engine.tick()
        detail = store.detail(task["id"])
        assert detail["task"]["status"] == "cancelled"
        assert detail["task"]["active_agents"] == 0
        assert not any(x["status"] in {"running", "queued"} for x in detail["items"])
        assert any(c["steering"] == ["Include source checks"] for _, c in provider.calls)
    finally:
        await engine.close()
        store.close()


def test_ownership_idempotency_and_atomic_claims(tmp_path):
    store = Store(tmp_path / "state.db")
    a = store.create_task("a", "task", idempotency_key="same")
    assert store.create_task("a", "task", idempotency_key="same")["id"] == a["id"]
    assert store.get_task(a["id"], "b") is None
    assert store.detail(a["id"], "b") is None
    assert store.control(a["id"], "cancel", "b") is None
    assert store.steer(a["id"], "override", "b") is None
    with pytest.raises(ValueError):
        store.create_task("a", "different", idempotency_key="same")
    one = store.claim("one", global_limit=1)
    assert store.claim("two", global_limit=1) is None
    store.fail(one, "recoverable", delay=0)
    two = store.claim("two", global_limit=1)
    assert two["attempt"] == 2
    # A stale response may account for spend but may never publish effects.
    response = {"content": "late", "usage": {"total_tokens": 5}}
    assert not store.checkpoint_response(one, response)
    assert not store.complete(one, response)
    assert store.checkpoint_response(two, {"content": "current", "usage": {"total_tokens": 7}})
    assert store.get_task(a["id"])["tokens_used"] == 12
    assert store.checkpoint_response(two, {"content": "current", "usage": {"total_tokens": 7}})
    assert store.get_task(a["id"])["tokens_used"] == 12
    store.close()


@pytest.mark.asyncio
async def test_retry_budget_and_invalid_plan_are_explicit(tmp_path):
    class BrokenProvider:
        async def execute(self, *args, **kwargs):
            return {"content": '{"items":[]}', "usage": {"total_tokens": 1100}}
    store = Store(tmp_path / "state.db")
    task = store.create_task("owner", "task", token_budget=1000)
    engine = Engine(store, BrokenProvider())
    try:
        await drive(engine, [task["id"]])
        assert store.get_task(task["id"])["status"] == "budget_exhausted"
        assert store.get_task(task["id"])["tokens_used"] == 1100
    finally:
        await engine.close()
        store.close()
    with pytest.raises(ValueError, match="cycle"):
        validate_plan(json.dumps({"items": [{"id": "a", "title": "x", "prompt": "x", "dependencies": ["a"]}]}))


@pytest.mark.asyncio
async def test_abrupt_process_death_recovers_wal_and_paid_response(tmp_path):
    path = tmp_path / "crash.db"
    store = Store(path)
    task = store.create_task("owner", "Retain work through a process crash", 2)
    code = '''
import json,os,sys
from swarm.store import Store
s=Store(sys.argv[1])
i=s.claim("killed-worker",lease_seconds=-1)
r={"content":json.dumps({"items":[{"id":"a","title":"Write evidence","prompt":"Write","dependencies":[]}]}),"usage":{"total_tokens":77}}
s.checkpoint_response(i,r)
os._exit(9)
'''
    child = subprocess.run([sys.executable, "-c", code, str(path)], timeout=10)
    assert child.returncode == 9
    engine = Engine(store, EvidenceProvider())
    try:
        await drive(engine, [task["id"]])
        assert store.get_task(task["id"])["status"] == "completed"
        assert store.get_task(task["id"])["tokens_used"] == 277
        assert len([x for x in store.detail(task["id"])["items"] if x["role"] == "planner"]) == 1
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
async def test_uncertain_paid_error_blocks_and_records_usage(tmp_path):
    from swarm.provider import ProviderError
    class Uncertain:
        async def execute(self, *args, **kwargs):
            raise ProviderError("Provider request outcome is uncertain", usage={"total_tokens":123,"prompt_tokens":100,"completion_tokens":23}, usage_uncertain=True)
    store = Store(tmp_path / "state.db")
    task = store.create_task("owner", "Paid task")
    engine = Engine(store, Uncertain())
    try:
        await drive(engine, [task["id"]])
        current = store.get_task(task["id"])
        assert current["status"] == "blocked"
        assert current["tokens_used"] == 123
        assert "uncertain" in current["blocker"]
        assert store.detail(task["id"])["items"][0]["attempt"] == 1
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
async def test_paid_final_response_publishes_at_budget_cap_after_restart(tmp_path):
    store = Store(tmp_path / "state.db")
    task = store.create_task("owner", "Preserve final result", token_budget=1000)
    with store.tx() as db:
        db.execute("UPDATE items SET role='verifier' WHERE task_id=?", (task["id"],))
    item = store.claim("crashed", lease_seconds=-1)
    response = {"content": json.dumps({"accepted": True, "result": "The final paid result", "issues": []}), "usage": {"total_tokens": 1000}}
    store.checkpoint_response(item, response)
    provider = EvidenceProvider()
    engine = Engine(store, provider)
    try:
        await drive(engine, [task["id"]])
        assert store.get_task(task["id"])["status"] == "completed"
        assert store.get_task(task["id"])["result"] == "The final paid result"
        assert provider.calls == []
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
async def test_cancellation_retains_known_tool_round_usage(tmp_path):
    from swarm.provider import ProviderError
    entered = asyncio.Event()
    class PartialUsage:
        async def execute(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise ProviderError("Cancelled with an in-flight request", usage={"total_tokens":100,"prompt_tokens":80,"completion_tokens":20}, usage_uncertain=True) from None
    store = Store(tmp_path / "state.db")
    task = store.create_task("owner", "Track interrupted work")
    engine = Engine(store, PartialUsage())
    try:
        await engine.tick()
        await asyncio.wait_for(entered.wait(), timeout=3)
        store.control(task["id"], "cancel", "owner")
        executions = [execution for item, execution in engine.running.values() if item["task_id"] == task["id"]]
        await engine.tick()
        # Let the cancelled execution persist its receipt before another tick
        # could issue a second cancellation during asynchronous graph cleanup.
        await asyncio.wait_for(asyncio.gather(*executions, return_exceptions=True), timeout=3)
        await engine.tick()
        detail = store.detail(task["id"])
        assert detail["task"]["status"] == "cancelled"
        assert detail["task"]["tokens_used"] == 100
        assert detail["task"]["tokens_uncertain"] > 0
        assert any(x["kind"] == "usage_uncertain" for x in detail["events"])
    finally:
        await engine.close()
        store.close()


def test_real_verifier_prose_json_regression_and_ambiguous_decisions_rejected():
    decision = '{"accepted":true,"result":"17*23 = 391; 29*31 = 899; sum = 1290","issues":[]}'
    for wrapped in [decision, "All verifications pass.\n\n```json\n" + decision + "\n```", "Checks complete.\n" + decision]:
        assert validate_verification(wrapped)["accepted"] is True
    for malformed in [
        'Everything is accepted and done.',
        '{"accepted":true,"accepted":false,"result":"x","issues":[]}',
        'Check: ' + decision + '\n' + '{"accepted":false,"result":"","issues":["missing"]}',
        '{"outer":' + decision,  # missing root closing brace
        '{"accepted":"true","result":"x","issues":[]}',
        '{"accepted":true,"result":"x","issues":["still incomplete"]}',
    ]:
        with pytest.raises(ValueError):
            validate_verification(malformed)


def test_dynamic_allowance_never_spends_other_agents_reservations(tmp_path):
    store = Store(tmp_path / "state.db")
    task = store.create_task("owner", "Allow budget growth", agent_limit=2, token_budget=50000)
    with store.tx() as db:
        store._insert_item(db, task["id"], "Second", "worker", "second")
    a = store.claim("one")
    b = store.claim("two")
    assert a["reserved_tokens"] == b["reserved_tokens"] == 20000
    assert store.extend_reservation(a, 40000) == 30000
    assert store.extend_reservation(b, 40000) == 20000
    assert a["reserved_tokens"] + b["reserved_tokens"] == task["token_budget"]
    store.control(task["id"], "cancel", "owner")
    assert store.extend_reservation(a, 50000) == 0
    store.close()


@pytest.mark.asyncio
async def test_graceful_restart_recovers_real_provider_cancellation_contract(tmp_path):
    from swarm.provider import ProviderError
    entered = asyncio.Event()
    class InFlight:
        async def execute(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise ProviderError("Shutdown interrupted provider request", usage={"total_tokens":100,"prompt_tokens":80,"completion_tokens":20}, usage_uncertain=True) from None
    path = tmp_path / "restart.db"
    store = Store(path)
    task = store.create_task("owner", "Restart durable paid worker", agent_limit=2)
    engine = Engine(store, InFlight())
    await engine.tick()
    await asyncio.wait_for(entered.wait(), timeout=3)
    await engine.close()
    assert store.get_task(task["id"])["status"] == "queued"
    assert store.get_task(task["id"])["tokens_used"] == 100
    assert store.get_task(task["id"])["tokens_uncertain"] > 0
    store.close()
    store = Store(path)
    resumed = Engine(store, EvidenceProvider())
    try:
        await drive(resumed, [task["id"]])
        assert store.get_task(task["id"])["status"] == "completed"
        assert store.detail(task["id"])["items"][0]["attempt"] == 2
    finally:
        await resumed.close()
        store.close()


@pytest.mark.asyncio
async def test_live_local_assignment_not_reclaimed_after_delayed_heartbeat(tmp_path):
    entered = asyncio.Event()
    class Waiting:
        async def execute(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
    store = Store(tmp_path / "lease.db")
    task = store.create_task("owner", "Retain local execution", agent_limit=1)
    engine = Engine(store, Waiting())
    try:
        await engine.tick()
        await asyncio.wait_for(entered.wait(), timeout=3)
        with store.tx() as db:
            db.execute("UPDATE items SET lease_until=0 WHERE task_id=?", (task["id"],))
        await engine.tick()
        detail = store.detail(task["id"])
        assert detail["items"][0]["attempt"] == 1
        assert detail["task"]["active_agents"] == 1
        assert not any(e["kind"] == "recovered" for e in detail["events"])
        assert len(engine.running) == 1
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
async def test_scheduler_survives_transient_database_fault_and_finishes_queue(tmp_path):
    import sqlite3
    store = Store(tmp_path / "supervised.db")
    task = store.create_task("owner", "Survive transient scheduler failure", agent_limit=2)
    original = store.claim
    calls = 0
    def intermittent(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("Simulated transient database busy")
        return original(*args, **kwargs)
    store.claim = intermittent
    engine = Engine(store, EvidenceProvider(), poll_seconds=0.01)
    stop = asyncio.Event()
    running = asyncio.create_task(engine.run_forever(stop))
    try:
        async with asyncio.timeout(5):
            while store.get_task(task["id"])["status"] != "completed":
                assert not running.done(), "Scheduler supervision unexpectedly stopped"
                await asyncio.sleep(0.01)
        assert calls > 1 and engine.last_scheduler_error is None
    finally:
        stop.set()
        await running
        store.close()


@pytest.mark.asyncio
async def test_real_provider_empty_final_completion_retries_and_accounts_both_calls(tmp_path):
    import httpx
    from swarm.provider import DeepSeekProvider
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        content = "" if calls == 1 else json.dumps({"accepted":True,"result":"Independent verification recovered after an empty completion.","issues":[]})
        return httpx.Response(200,json={"model":"deepseek-v4-flash","choices":[{"message":{"role":"assistant","content":content},"finish_reason":"stop"}],
                                       "usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}})
    store = Store(tmp_path / "empty-completion.db")
    task = store.create_task("owner", "Recover an empty verifier response", max_attempts=3)
    with store.tx() as db:
        db.execute("UPDATE items SET role='verifier' WHERE task_id=?", (task["id"],))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engine = Engine(store, DeepSeekProvider("test",client=client))
        try:
            await drive(engine, [task["id"]])
            detail = store.detail(task["id"])
            assert calls == 2 and detail["task"]["status"] == "completed"
            assert detail["items"][0]["attempt"] == 2
            assert detail["task"]["tokens_used"] == 30
            assert detail["task"]["tokens_uncertain"] == 0
            assert any(e["kind"] == "retry" and "empty completion" in e["message"] for e in detail["events"])
        finally:
            await engine.close()
            store.close()


def test_verifier_sees_committed_operational_proof_and_cross_worker_handoffs(tmp_path):
    store = Store(tmp_path / "verification-ledger.db")
    task = store.create_task("owner", "Verify actual operations")
    with store.tx() as db:
        db.execute("DELETE FROM items WHERE task_id=?", (task["id"],))
        a = store._insert_item(db,task["id"],"Source","worker","source")
        b = store._insert_item(db,task["id"],"Recipient","worker","recipient",[a])
        v = store._insert_item(db,task["id"],"Verify","verifier","verify",[a,b])
        db.execute("UPDATE items SET status='completed',attempt=1,output='Unsupported prose alone' WHERE id IN (?,?)",(a,b))
        store._event(db,task["id"],"tool","Executed calculate.",a,"source-agent")
        store._event(db,task["id"],"tool","Executed calculate.",a,"source-agent")
        store._artifact(db,task["id"],a,"proof.md","verified calculation")
        db.execute("INSERT INTO handoffs(task_id,source_item_id,target_item_id,message,created_at) VALUES(?,?,?,?,?)",(task["id"],a,b,"Calculation result ready", "2026-09-05T16:00:00+00:00"))
    verifier = store.claim("engine")
    assert verifier["id"] == v
    context = store.context(verifier)
    proof = next(x for x in context["execution_evidence"] if x["id"] == a)
    assert proof["executed_tool_counts"] == {"calculate":2}
    assert proof["committed_handoff_targets"] == [b]
    assert proof["artifact_ids"] and context["handoffs"][0]["target_item_id"] == b
    # Another worker must still receive only its own targeted messages.
    other = {**verifier, "id":"unrelated", "role":"worker", "dependencies":[]}
    assert store.context(other)["handoffs"] == []
    store.close()
