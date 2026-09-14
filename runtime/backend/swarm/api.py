"""Authenticated single-owner API and persistent background worker.

Configuration:
SWARM_OWNER_EMAIL and SWARM_PASSWORD_HASH (scrypt$16384$8$1$salt_hex$key_hex;
salt >=16 bytes, derived key 32 bytes), SWARM_PUBLIC_ORIGIN (frontend origin),
SWARM_DB_PATH (persistent local-volume filename), DEEPSEEK_API_KEY.
SWARM_COOKIE_SECURE defaults true; disable only for local HTTP development.
SWARM_SESSION_HOURS defaults 24; SWARM_GLOBAL_AGENT_LIMIT defaults 8.
No credentials are generated, embedded in browser assets, or logged.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .engine import Engine
from .provider import DeepSeekProvider, MODEL
from .store import Store
from .tool_registry import inventory
from .observability import flush_traces, health as observability_health
from .adaptive import assess

logger = logging.getLogger(__name__)
COOKIE_NAME = "nexus_session"


@dataclass
class Settings:
    owner_email: str = ""
    password_hash: str = ""
    public_origin: str = ""
    db_path: str = "var/swarm.db"
    cookie_secure: bool = True
    session_hours: int = 24
    global_agent_limit: int = 8

    @classmethod
    def from_env(cls):
        return cls(owner_email=os.environ.get("SWARM_OWNER_EMAIL", "").strip().lower(),
                   password_hash=os.environ.get("SWARM_PASSWORD_HASH", ""),
                   public_origin=os.environ.get("SWARM_PUBLIC_ORIGIN", "").rstrip("/"),
                   db_path=os.environ.get("SWARM_DB_PATH", "var/swarm.db"),
                   cookie_secure=os.environ.get("SWARM_COOKIE_SECURE", "true").lower() != "false",
                   session_hours=max(1, min(int(os.environ.get("SWARM_SESSION_HOURS", "24")), 168)),
                   global_agent_limit=max(1, min(int(os.environ.get("SWARM_GLOBAL_AGENT_LIMIT", "8")), 32)))

    @property
    def owner_id(self):
        return hashlib.sha256(self.owner_email.encode()).hexdigest()[:32]

    @property
    def auth_epoch(self):
        return hashlib.sha256((self.owner_email + self.password_hash).encode()).hexdigest()

    @property
    def configured(self):
        try:
            parse_password_hash(self.password_hash)
            parsed = urlsplit(self.public_origin)
            return bool(self.owner_email and parsed.hostname and parsed.scheme in {"https", "http"}
                        and not parsed.username and not parsed.password and parsed.path in {"", "/"}
                        and not parsed.query and not parsed.fragment
                        and (not self.cookie_secure or parsed.scheme == "https"))
        except (ValueError, TypeError):
            return False


def parse_password_hash(encoded):
    algorithm, n, r, p, salt, digest = encoded.split("$")
    if (algorithm, n, r, p) != ("scrypt", "16384", "8", "1"):
        raise ValueError("Unsupported password hash format")
    salt, digest = bytes.fromhex(salt), bytes.fromhex(digest)
    if not 16 <= len(salt) <= 64 or len(digest) != 32:
        raise ValueError("Invalid password hash lengths")
    return salt, digest


def verify_password(password, encoded):
    salt, expected = parse_password_hash(encoded)
    actual = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return hmac.compare_digest(actual, expected)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class TaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=20000)
    agent_limit: int = Field(default=8, ge=1, le=20, strict=True)
    allocation_mode: Literal['auto','manual'] = 'manual'
    max_attempts: int = Field(default=3, ge=1, le=8, strict=True)
    token_budget: int = Field(default=250000, ge=1000, le=1000000, strict=True)


class ResourceBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['auto','manual']
    agent_ceiling: int = Field(ge=1,le=20,strict=True)
    token_ceiling: int = Field(ge=1000,le=1000000,strict=True)


class SteeringBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=10000)


class BudgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_budget: int = Field(ge=1000, le=1000000, strict=True)


def create_app(settings: Settings | None = None, *, provider=None, start_engine=True):
    settings = settings or Settings.from_env()
    provider = provider or DeepSeekProvider()

    @asynccontextmanager
    async def lifespan(application):
        store = Store(settings.db_path)
        with store.tx() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY,owner_id TEXT NOT NULL,expires_at REAL NOT NULL,auth_epoch TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS request_limits (id INTEGER PRIMARY KEY AUTOINCREMENT,bucket TEXT NOT NULL,created_at REAL NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS limits_lookup ON request_limits(bucket,created_at)")
        application.state.store = store
        application.state.settings = settings
        application.state.provider = provider
        engine = Engine(store, provider, global_limit=settings.global_agent_limit)
        application.state.engine = engine
        stop = asyncio.Event()
        background = asyncio.create_task(engine.run_forever(stop)) if start_engine else None
        try:
            yield
        finally:
            stop.set()
            try:
                if background:
                    await background
                else:
                    await engine.close()
            finally:
                store.close()
                await flush_traces()

    application = FastAPI(title="Swarm Nexus API", version="1.0.0", lifespan=lifespan,
                          docs_url=None, redoc_url=None, openapi_url=None)

    @application.middleware("http")
    async def protect_boundary(request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin", "")
            allowed = {settings.public_origin, *filter(None, os.environ.get('NEXUS_EXTRA_ORIGINS','').split(','))}
            if not settings.public_origin or origin not in allowed:
                response = JSONResponse({"detail": "Request origin is not allowed."}, status_code=403)
            elif request.headers.get("sec-fetch-site") == "cross-site":
                response = JSONResponse({"detail": "Cross-site requests are not allowed."}, status_code=403)
            elif request.headers.get("content-length", "0").isdigit() and int(request.headers.get("content-length", "0")) > 65536:
                response = JSONResponse({"detail": "Request body is too large."}, status_code=413)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
                                 "Permissions-Policy": "camera=(), microphone=(), geolocation=()"})
        if settings.cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default response can reflect passwords or submitted content.
        return JSONResponse({"detail": "Request fields are invalid or outside the supported limits."}, status_code=422)

    @application.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)[:500]}, status_code=409)

    @application.exception_handler(Exception)
    async def server_error(request, exc):
        logger.error("API operation failed (%s)", type(exc).__name__)
        return JSONResponse({"detail": "The operation could not be completed. Saved work has been retained."}, status_code=500)

    def session_user(request):
        token = request.cookies.get(COOKIE_NAME, "")
        if not 32 <= len(token) <= 128 or not settings.configured:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        store = request.app.state.store
        with store.lock:
            row = store.db.execute("SELECT * FROM sessions WHERE token_hash=? AND expires_at>? AND auth_epoch=? AND owner_id=?", (digest, time.time(), settings.auth_epoch, settings.owner_id)).fetchone()
        return {"id": row["owner_id"], "email": settings.owner_email} if row else None

    def require_user(request: Request):
        user = session_user(request)
        if not user:
            raise HTTPException(401, "Sign in to access this workspace.")
        return user

    def rate_limit(store, bucket, count, seconds, record=True):
        with store.tx() as db:
            db.execute("DELETE FROM request_limits WHERE created_at<?", (time.time() - 86400,))
            used = db.execute("SELECT COUNT(*) FROM request_limits WHERE bucket=? AND created_at>?", (bucket, time.time()-seconds)).fetchone()[0]
            if used >= count:
                raise HTTPException(429, "Too many attempts. Wait before trying again.", headers={"Retry-After": str(seconds)})
            if record:
                db.execute("INSERT INTO request_limits(bucket,created_at) VALUES(?,?)", (bucket, time.time()))

    @application.get("/api/health")
    async def health(request: Request, response: Response):
        engine = request.app.state.engine
        engine_task_ok = not start_engine or (not engine.closed and engine.last_scheduler_error is None)
        if not engine_task_ok:
            response.status_code = 503
        return {"status": "ok" if engine_task_ok else "degraded", "provider_configured": provider.configured,
                "service":"swarm-nexus", "release":os.environ.get('NEXUS_RELEASE','development'),
                "auth_configured": settings.configured, "model": MODEL,
                "persistence": "sqlite_wal_single_replica", "observability": observability_health()}

    @application.post('/api/assess')
    async def assessment(body: TaskBody, request: Request, user=Depends(require_user)):
        return {**assess(body.prompt,body.agent_limit,body.token_budget),'global_capacity':settings.global_agent_limit}

    @application.post('/api/tasks/{task_id}/resources')
    async def resources(task_id: str, body: ResourceBody, request: Request, user=Depends(require_user)):
        result=request.app.state.store.configure_resources(task_id,body.mode,body.agent_ceiling,body.token_ceiling,user['id'])
        if result is None:
            raise HTTPException(404,'Mission was not found.')
        return result

    @application.get("/api/session")
    async def session(request: Request):
        return {"user": session_user(request)}

    @application.post("/api/login")
    async def login(body: LoginBody, request: Request, response: Response):
        if not settings.configured:
            raise HTTPException(503, "Workspace authentication has not been configured.")
        store = request.app.state.store
        address = request.client.host if request.client else "unknown"
        bucket = "login:" + hashlib.sha256(address.encode()).hexdigest()
        rate_limit(store, bucket, 6, 900, record=False)
        rate_limit(store, "login:global", 30, 900, record=False)
        password_ok = verify_password(body.password, settings.password_hash)
        email_ok = hmac.compare_digest(body.email.strip().lower().encode(), settings.owner_email.encode())
        if not password_ok or not email_ok:
            rate_limit(store, bucket, 6, 900)
            rate_limit(store, "login:global", 30, 900)
            raise HTTPException(401, "Email or password is incorrect.")
        token = secrets.token_urlsafe(48)
        max_age = settings.session_hours * 3600
        with store.tx() as db:
            db.execute("DELETE FROM sessions WHERE expires_at<? OR auth_epoch<>?", (time.time(), settings.auth_epoch))
            db.execute("INSERT INTO sessions(token_hash,owner_id,expires_at,auth_epoch) VALUES(?,?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), settings.owner_id, time.time()+max_age, settings.auth_epoch))
        response.set_cookie(COOKIE_NAME, token, max_age=max_age, httponly=True, secure=settings.cookie_secure,
                            samesite="lax", path="/")
        return {"user": {"id": settings.owner_id, "email": settings.owner_email}}

    @application.post("/api/logout")
    async def logout(request: Request, response: Response):
        token = request.cookies.get(COOKIE_NAME, "")
        with request.app.state.store.tx() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        response.delete_cookie(COOKIE_NAME, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")
        return {"user": None}

    @application.get("/api/tasks")
    async def tasks(request: Request, user=Depends(require_user)):
        return {"tasks": request.app.state.store.list_tasks(user["id"])}

    @application.post("/api/tasks", status_code=201)
    async def create_task(body: TaskBody, request: Request, user=Depends(require_user)):
        if not provider.configured:
            raise HTTPException(503, "DeepSeek is not connected. Add the server-side credential before starting paid work.")
        store = request.app.state.store
        key = request.headers.get("idempotency-key")
        if key is not None and not 1 <= len(key) <= 128:
            raise HTTPException(422, "Idempotency key must contain 1–128 characters.")
        # Idempotent retries return the original task without consuming limits.
        if key:
            with store.lock:
                existing = store.db.execute("SELECT id FROM tasks WHERE owner_id=? AND idempotency_key=?", (user["id"], key)).fetchone()
            if existing:
                return store.create_task(user["id"], **body.model_dump(), idempotency_key=key)
        with store.lock:
            pending = store.db.execute("SELECT COUNT(*) FROM tasks WHERE owner_id=? AND status IN ('queued','running','paused')", (user["id"],)).fetchone()[0]
        if pending >= 20:
            raise HTTPException(429, "Workspace has 20 unfinished tasks. Cancel or finish work before adding more.")
        rate_limit(store, "create:" + user["id"], 10, 60)
        return store.create_task(user["id"], **body.model_dump(), idempotency_key=key)

    @application.get("/api/tasks/{task_id}")
    async def task_detail(task_id: str, request: Request, user=Depends(require_user)):
        result = request.app.state.store.detail(task_id, user["id"])
        if result is None:
            raise HTTPException(404, "Task was not found.")
        return result

    @application.post("/api/tasks/{task_id}/steer")
    async def steer(task_id: str, body: SteeringBody, request: Request, user=Depends(require_user)):
        result = request.app.state.store.steer(task_id, body.message, user["id"])
        if result is None:
            raise HTTPException(404, "Task was not found.")
        return result

    @application.post("/api/tasks/{task_id}/budget")
    async def increase_budget(task_id: str, body: BudgetBody, request: Request, user=Depends(require_user)):
        store = request.app.state.store
        if store.get_task(task_id, user["id"]) is None:
            raise HTTPException(404, "Task was not found.")
        # No await between checking local executions and the atomic transition:
        # late cancellation receipts must settle before admitting new spending.
        if any(item["task_id"] == task_id for item, _ in request.app.state.engine.running.values()):
            raise HTTPException(409, "Previous provider execution is settling. Retry shortly.")
        with store.lock:
            pending = store.db.execute("SELECT COUNT(*) FROM tasks WHERE owner_id=? AND status IN ('queued','running','paused')", (user["id"],)).fetchone()[0]
        if pending >= 20:
            raise HTTPException(429, "Workspace has 20 unfinished tasks. Cancel or finish work before resuming more.")
        return store.increase_budget(task_id, body.token_budget, user["id"])

    @application.post("/api/tasks/{task_id}/{action}")
    async def control(task_id: str, action: str, request: Request, user=Depends(require_user)):
        if action not in {"pause", "resume", "cancel"}:
            raise HTTPException(404, "Action was not found.")
        result = request.app.state.store.control(task_id, action, user["id"])
        if result is None:
            raise HTTPException(404, "Task was not found.")
        return result

    @application.get("/api/tasks/{task_id}/artifacts/{artifact_id}")
    async def artifact(task_id: str, artifact_id: str, request: Request, user=Depends(require_user)):
        result = request.app.state.store.get_artifact(task_id, artifact_id, user["id"])
        if result is None:
            raise HTTPException(404, "Artifact was not found.")
        # Download as plain bytes; never execute an agent-authored HTML/SVG file.
        return Response(result["content"].encode(), media_type="application/octet-stream",
                        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(result["name"], safe="")})

    @application.get("/api/tools")
    async def tools(user=Depends(require_user)):
        return {"tools": inventory(),
                "model": MODEL, "capacity": {"max_agents_per_task": 20, "max_global_agents": settings.global_agent_limit, "includes_coordinator_and_verifier": True},
                "limits": {"max_unfinished_tasks": 20, "max_attempts": 8, "max_token_budget": 1000000,
                           "execution": "Read-only public web, arithmetic, staged text artifacts and internal handoffs. No host shell or external publishing."}}

    return application


app = create_app()
