import hashlib
import time

import pytest
from fastapi.testclient import TestClient

from swarm.api import COOKIE_NAME, Settings, create_app


class ConfiguredProvider:
    configured = True


@pytest.fixture
def settings(tmp_path):
    salt = bytes.fromhex("aabbccddeeff00112233445566778899")
    derived = hashlib.scrypt(b"test password only", salt=salt, n=16384, r=8, p=1, dklen=32)
    return Settings(owner_email="owner@example.test", password_hash=f"scrypt$16384$8$1${salt.hex()}${derived.hex()}",
                    public_origin="https://swarm.example.test", db_path=str(tmp_path / "api.db"))


def make_client(settings):
    return TestClient(create_app(settings, provider=ConfiguredProvider(), start_engine=False), base_url=settings.public_origin)


def login(client, settings):
    return client.post("/api/login", headers={"Origin": settings.public_origin}, json={"email": settings.owner_email, "password": "test password only"})


def test_auth_csrf_cookie_security_and_logout_revocation(settings):
    with make_client(settings) as client:
        assert client.get("/api/health").json()["auth_configured"] is True
        assert client.get("/api/session").json() == {"user": None}
        assert client.get("/api/tasks").status_code == 401
        body = {"email": settings.owner_email, "password": "test password only"}
        assert client.post("/api/login", json=body).status_code == 403
        assert client.post("/api/login", headers={"Origin": "https://evil.example"}, json=body).status_code == 403
        response = login(client, settings)
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
        token = client.cookies.get(COOKIE_NAME)
        with client.app.state.store.lock:
            stored = client.app.state.store.db.execute("SELECT token_hash FROM sessions").fetchone()[0]
        assert stored != token and stored == hashlib.sha256(token.encode()).hexdigest()
        assert client.get("/api/tasks").status_code == 200
        assert client.post("/api/logout", headers={"Origin": settings.public_origin}).status_code == 200
        client.cookies.set(COOKIE_NAME, token)
        assert client.get("/api/tasks").status_code == 401


def test_sessions_survive_restart_expire_and_password_rotation(settings):
    with make_client(settings) as client:
        assert login(client, settings).status_code == 200
        token = client.cookies.get(COOKIE_NAME)
    with make_client(settings) as resumed:
        resumed.cookies.set(COOKIE_NAME, token)
        assert resumed.get("/api/session").json()["user"]["email"] == settings.owner_email
        with resumed.app.state.store.tx() as db:
            db.execute("UPDATE sessions SET expires_at=?", (time.time()-1,))
        assert resumed.get("/api/tasks").status_code == 401
        assert login(resumed, settings).status_code == 200
        settings.password_hash = settings.password_hash[:-2] + "00"
        assert resumed.get("/api/tasks").status_code == 401


def test_budget_endpoint_auth_origin_bounds_and_atomic_resume(settings):
    with make_client(settings) as client:
        headers = {"Origin": settings.public_origin}
        assert client.post("/api/tasks/missing/budget", headers=headers, json={"token_budget": 1000000}).status_code == 401
        login(client, settings)
        store = client.app.state.store
        foreign = store.create_task("foreign", "Private")
        assert client.post(f"/api/tasks/{foreign['id']}/budget", headers=headers, json={"token_budget": 1000000}).status_code == 404
        task = client.post("/api/tasks", headers=headers, json={"prompt": "Budget validation", "token_budget": 1000}).json()
        path = f"/api/tasks/{task['id']}/budget"
        assert client.post(path, json={"token_budget": 2000}).status_code == 403
        assert client.post(path, headers={"Origin": "https://evil.test"}, json={"token_budget": 2000}).status_code == 403
        for state in ("queued", "running", "paused", "completed", "cancelled", "blocked"):
            with store.tx() as db:
                db.execute("UPDATE tasks SET status=? WHERE id=?", (state, task["id"]))
            assert client.post(path, headers=headers, json={"token_budget": 2000}).status_code == 409
        with store.tx() as db:
            store._stop(db, task["id"], "budget_exhausted", "Token budget reached")
        before = store.detail(task["id"])
        for amount, status in ((1000, 409), (999, 422), (1000001, 422), (True, 422), ("2000", 422)):
            assert client.post(path, headers=headers, json={"token_budget": amount}).status_code == status
            assert store.detail(task["id"]) == before
        engine = client.app.state.engine
        engine.running["settling"] = ({"task_id": task["id"]}, None)
        try:
            assert client.post(path, headers=headers, json={"token_budget": 2000}).status_code == 409
            assert store.detail(task["id"]) == before
        finally:
            engine.running.clear()
        response = client.post(path, headers=headers, json={"token_budget": 1000000})
        assert response.status_code == 200
        assert response.json()["status"] == "queued" and response.json()["token_budget"] == 1000000
        assert response.json()["tokens_used"] == 0
        assert client.post(path, headers=headers, json={"token_budget": 1000000}).status_code == 409


def test_budget_resume_honors_workspace_capacity(settings):
    with make_client(settings) as client:
        login(client, settings)
        headers = {"Origin": settings.public_origin}
        task = client.post("/api/tasks", headers=headers, json={"prompt": "Resume within capacity", "token_budget": 1000}).json()
        store = client.app.state.store
        with store.tx() as db:
            owner = db.execute("SELECT owner_id FROM tasks WHERE id=?", (task["id"],)).fetchone()[0]
            store._stop(db, task["id"], "budget_exhausted", "Token budget reached")
        for i in range(20):
            store.create_task(owner, f"Queued task {i}")
        before = store.detail(task["id"])
        assert client.post(f"/api/tasks/{task['id']}/budget", headers=headers, json={"token_budget": 2000}).status_code == 429
        assert store.detail(task["id"]) == before


def test_every_task_route_checks_ownership_and_idempotency(settings):
    with make_client(settings) as client:
        login(client, settings)
        headers = {"Origin": settings.public_origin, "Idempotency-Key": "request-1"}
        body = {"prompt": "Produce evidence", "agent_limit": 2}
        created = client.post("/api/tasks", headers=headers, json=body)
        assert created.status_code == 201
        task = created.json()
        assert client.post("/api/tasks", headers=headers, json=body).json()["id"] == task["id"]
        assert client.post("/api/tasks", headers=headers, json={"prompt": "Different"}).status_code == 409
        foreign = client.app.state.store.create_task("another-owner", "Private task")
        for path in [f"/api/tasks/{foreign['id']}", f"/api/tasks/{foreign['id']}/artifacts/missing"]:
            assert client.get(path).status_code == 404
        for action in ["pause", "resume", "cancel", "steer"]:
            response = client.post(f"/api/tasks/{foreign['id']}/{action}", headers=headers, json={"message": "Change it"})
            assert response.status_code == 404
        assert len(client.get("/api/tasks").json()["tasks"]) == 1
        assert client.post(f"/api/tasks/{task['id']}/pause", headers=headers).json()["status"] == "paused"
        assert client.post(f"/api/tasks/{task['id']}/steer", headers=headers, json={"message": "Use reliable evidence"}).status_code == 200
        assert client.post(f"/api/tasks/{task['id']}/resume", headers=headers).json()["status"] == "queued"
        assert client.post(f"/api/tasks/{task['id']}/cancel", headers=headers).json()["status"] == "cancelled"


def test_persistent_login_throttling_and_safe_validation(settings):
    with make_client(settings) as client:
        for _ in range(6):
            assert client.post("/api/login", headers={"Origin": settings.public_origin}, json={"email": "wrong", "password": "wrong"}).status_code == 401
        response = login(client, settings)
        assert response.status_code == 429
        assert response.headers["retry-after"] == "900"
    with make_client(settings) as restarted:
        assert login(restarted, settings).status_code == 429
        response = restarted.post("/api/login", headers={"Origin": settings.public_origin}, json={"email": "x", "password": "DO_NOT_REFLECT", "extra": True})
        assert response.status_code == 422 and "DO_NOT_REFLECT" not in response.text


def test_creation_rate_limit_and_downloaded_artifacts_are_inert(settings):
    with make_client(settings) as client:
        login(client, settings)
        headers = {"Origin": settings.public_origin}
        for _ in range(10):
            assert client.post("/api/tasks", headers=headers, json={"prompt": "task"}).status_code == 201
        assert client.post("/api/tasks", headers=headers, json={"prompt": "task"}).status_code == 429
        task = client.get("/api/tasks").json()["tasks"][0]
        store = client.app.state.store
        with store.tx() as db:
            store._artifact(db, task["id"], "fixture", "unsafe.html", "<script>alert(1)</script>", "text/html")
        artifact = client.get(f"/api/tasks/{task['id']}").json()["artifacts"][0]
        downloaded = client.get(f"/api/tasks/{task['id']}/artifacts/{artifact['id']}")
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"] == "application/octet-stream"
        assert downloaded.headers["content-disposition"].startswith("attachment;")
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in downloaded.headers["content-security-policy"]


def test_unhealthy_scheduler_returns_non_200_for_operational_health_checks(settings):
    app = create_app(settings, provider=ConfiguredProvider(), start_engine=True)
    with TestClient(app, base_url=settings.public_origin) as client:
        app.state.engine.closed = True
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"


def test_owned_detail_exposes_only_committed_handoffs_including_verifier_repairs(settings):
    with make_client(settings) as client:
        login(client,settings)
        task=client.post('/api/tasks',headers={'Origin':settings.public_origin},json={'prompt':'Create and check evidence'}).json()
        store=client.app.state.store
        planner=store.claim('api-handoff-test')
        plan=[{'id':'a','title':'Research','prompt':'Gather evidence','dependencies':[]},
              {'id':'b','title':'Synthesis','prompt':'Combine evidence','dependencies':['a']}]
        store.complete(planner,{'content':'Plan saved'},plan=plan)
        detail=store.detail(task['id'])
        target=next(item['id'] for item in detail['items'] if item['title']=='Synthesis')
        worker=store.claim('api-handoff-test')
        response={'content':'Source evidence saved',
                  'handoffs':[{'target_item_id':target,'message':'Use the checked source totals.'}],
                  'reasoning_content':'PRIVATE_PROVIDER_REASONING_MUST_NOT_APPEAR'}
        store.checkpoint_response(worker,response)
        assert client.get(f"/api/tasks/{task['id']}").json()['handoffs']==[]
        store.complete(worker,response)
        synthesis=store.claim('api-handoff-test')
        store.complete(synthesis,{'content':'Combined output'})
        verifier=store.claim('api-handoff-test')
        store.complete(verifier,{'content':'Verification found a missing citation'},
                       verification={'accepted':False,'result':'','issues':['Add the missing citation.']})
        visible=client.get(f"/api/tasks/{task['id']}")
        assert visible.status_code==200
        handoffs=visible.json()['handoffs']
        assert len(handoffs)==2
        assert all(set(h)=={'id','source_item_id','target_item_id','message','created_at'} for h in handoffs)
        assert handoffs[0]['source_item_id']==worker['id'] and handoffs[0]['target_item_id']==target
        assert handoffs[0]['message']=='Use the checked source totals.'
        repair=next(item for item in visible.json()['items'] if item['title']=='Repair verification gaps')
        assert handoffs[1]['source_item_id']==verifier['id'] and handoffs[1]['target_item_id']==repair['id']
        assert handoffs[1]['message']=='Add the missing citation.'
        assert handoffs[0]['id']<handoffs[1]['id'] and all(h['created_at'] for h in handoffs)
        assert 'PRIVATE_PROVIDER_REASONING_MUST_NOT_APPEAR' not in visible.text
        foreign=store.create_task('another-owner','Private task')
        with store.tx() as db:
            db.execute('INSERT INTO handoffs(task_id,source_item_id,target_item_id,message,created_at) VALUES(?,?,?,?,?)',
                       (foreign['id'],'private-source','private-target','OTHER_OWNER_PRIVATE_HANDOFF','2026-09-05T20:00:00+00:00'))
        assert client.get(f"/api/tasks/{foreign['id']}").status_code==404
        assert 'OTHER_OWNER_PRIVATE_HANDOFF' not in client.get(f"/api/tasks/{task['id']}").text
        client.post('/api/logout',headers={'Origin':settings.public_origin})
        assert client.get(f"/api/tasks/{task['id']}").status_code==401


def test_research_budget_default_and_explicit_limits_persist_across_restart(settings):
    with make_client(settings) as client:
        login(client,settings)
        headers={'Origin':settings.public_origin}
        default=client.post('/api/tasks',headers=headers,json={'prompt':'Research a device and verify its evidence'})
        explicit=client.post('/api/tasks',headers=headers,json={'prompt':'A small calculation','token_budget':50000})
        assert default.status_code==201 and explicit.status_code==201
        assert default.json()['token_budget']==250000
        assert explicit.json()['token_budget']==50000
        direct=client.app.state.store.create_task('internal-owner','A task created by a server integration')
        assert direct['token_budget']==250000
        token=client.cookies.get(COOKIE_NAME)
        expectations={default.json()['id']:250000,explicit.json()['id']:50000}
    with make_client(settings) as resumed:
        resumed.cookies.set(COOKIE_NAME,token)
        for task_id,expected in expectations.items():
            detail=resumed.get(f'/api/tasks/{task_id}')
            assert detail.status_code==200 and detail.json()['task']['token_budget']==expected
        assert resumed.app.state.store.get_task(direct['id'])['token_budget']==250000
