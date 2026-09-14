"""Durable single-service SQLite task board. All ownership and leases are atomic.

Deploy database and its WAL on one persistent local volume. Do not share SQLite
over NFS or run multiple cloud replicas. Model calls are at-least-once after a
crash; artifact publication and assignment transitions are idempotent.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .adaptive import assess, growth, plan_assessment


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


TERMINAL = {"completed", "cancelled", "failed", "blocked", "budget_exhausted"}


class Store:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript("""
        PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, prompt TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued', agent_limit INTEGER NOT NULL,
          max_attempts INTEGER NOT NULL, token_budget INTEGER NOT NULL,
          tokens_used INTEGER NOT NULL DEFAULT 0, estimated_cost_usd REAL NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result TEXT, blocker TEXT,
          idempotency_key TEXT, repair_round INTEGER NOT NULL DEFAULT 0,
          UNIQUE(owner_id,idempotency_key));
        CREATE TABLE IF NOT EXISTS items (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), title TEXT NOT NULL,
          role TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          dependencies TEXT NOT NULL DEFAULT '[]', assigned_agent TEXT, attempt INTEGER NOT NULL DEFAULT 0,
          output TEXT, error TEXT, lease_token TEXT, lease_until REAL, available_at REAL NOT NULL DEFAULT 0,
          response TEXT, usage_recorded INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
          reserved_tokens INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS items_task ON items(task_id,status);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
          item_id TEXT, agent TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS artifacts (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), item_id TEXT,
          name TEXT NOT NULL, media_type TEXT NOT NULL, size INTEGER NOT NULL,
          content TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(task_id,item_id,name));
        CREATE TABLE IF NOT EXISTS handoffs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
          source_item_id TEXT, target_item_id TEXT, message TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS usage_receipts (
          lease_token TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
          tokens INTEGER NOT NULL, estimated_cost_usd REAL NOT NULL);
        """)
        if "tokens_uncertain" not in {r[1] for r in self.db.execute("PRAGMA table_info(tasks)")}:
            self.db.execute("ALTER TABLE tasks ADD COLUMN tokens_uncertain INTEGER NOT NULL DEFAULT 0")
        self.db.executescript('''CREATE TABLE IF NOT EXISTS resource_policies (
          task_id TEXT PRIMARY KEY REFERENCES tasks(id), mode TEXT NOT NULL,
          agent_ceiling INTEGER NOT NULL, token_ceiling INTEGER NOT NULL,
          assessment TEXT NOT NULL, request_fingerprint TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS lease_reservations (
          lease_token TEXT PRIMARY KEY, task_id TEXT NOT NULL, tokens INTEGER NOT NULL,
          uncertain INTEGER NOT NULL DEFAULT 0);
        ''')

    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def close(self):
        self.db.close()

    def _event(self, db, task_id, kind, message, item_id=None, agent="coordinator"):
        db.execute("INSERT INTO events(task_id,item_id,agent,kind,message,created_at) VALUES(?,?,?,?,?,?)",
                   (task_id, item_id, agent, kind, str(message)[:10000], now()))

    def _task(self, db, task_id, owner_id=None):
        query = "SELECT * FROM tasks WHERE id=?" + (" AND owner_id=?" if owner_id is not None else "")
        row = db.execute(query, (task_id, owner_id) if owner_id is not None else (task_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["active_agents"] = db.execute("SELECT COUNT(*) FROM items WHERE task_id=? AND status='running'", (task_id,)).fetchone()[0]
        data['tokens_reserved'] = db.execute("SELECT COALESCE(SUM(reserved_tokens),0) FROM items WHERE task_id=? AND status='running'",(task_id,)).fetchone()[0]
        data.pop("idempotency_key", None)
        data.pop("owner_id", None)
        policy = db.execute('SELECT * FROM resource_policies WHERE task_id=?', (task_id,)).fetchone()
        data['resources'] = ({'mode': policy['mode'], 'agent_ceiling': policy['agent_ceiling'],
                              'token_ceiling': policy['token_ceiling'], 'assessment': json.loads(policy['assessment'])}
                             if policy else {'mode': 'manual', 'agent_ceiling': data['agent_limit'], 'token_ceiling': data['token_budget'], 'assessment': {}})
        return data

    def get_task(self, task_id, owner_id=None):
        with self.lock:
            return self._task(self.db, task_id, owner_id)

    def list_tasks(self, owner_id):
        with self.lock:
            ids = self.db.execute("SELECT id FROM tasks WHERE owner_id=? ORDER BY created_at DESC", (owner_id,)).fetchall()
            return [self._task(self.db, row[0]) for row in ids]

    def _insert_item(self, db, task_id, title, role, prompt, dependencies=(), item_id=None):
        item_id = item_id or uid()
        db.execute("INSERT INTO items(id,task_id,title,role,prompt,dependencies,created_at) VALUES(?,?,?,?,?,?,?)",
                   (item_id, task_id, title[:200], role, prompt, json.dumps(list(dependencies)), now()))
        return item_id

    def create_task(self, owner_id, prompt, agent_limit=4, max_attempts=3, token_budget=250000, idempotency_key=None, allocation_mode='manual'):
        if not 1 <= agent_limit <= 20 or not 1 <= max_attempts <= 8 or not 1000 <= token_budget <= 1000000:
            raise ValueError("Agent limit must be 1–20, attempts 1–8, and token budget 1,000–1,000,000.")
        if allocation_mode not in {'auto', 'manual'}:
            raise ValueError('Choose auto or manual resource allocation.')
        prompt = prompt.strip()
        if not prompt or len(prompt) > 20000:
            raise ValueError("Task must contain 1–20,000 characters.")
        fingerprint = hashlib.sha256(json.dumps([prompt, agent_limit, max_attempts, token_budget, allocation_mode]).encode()).hexdigest()
        assessment = assess(prompt, agent_limit, token_budget)
        ceiling_agents, ceiling_tokens = agent_limit, token_budget
        if allocation_mode == 'auto':
            agent_limit, token_budget = 1, assessment['initial_token_allocation']
        with self.tx() as db:
            if idempotency_key:
                existing = db.execute("SELECT * FROM tasks WHERE owner_id=? AND idempotency_key=?", (owner_id, idempotency_key)).fetchone()
                if existing:
                    policy = db.execute('SELECT request_fingerprint FROM resource_policies WHERE task_id=?', (existing['id'],)).fetchone()
                    if (policy and policy[0] != fingerprint) or (not policy and (existing["prompt"], existing["agent_limit"], existing["max_attempts"], existing["token_budget"]) != (prompt, agent_limit, max_attempts, token_budget)):
                        raise ValueError("Idempotency key was already used for another request.")
                    return self._task(db, existing["id"])
            task_id = uid()
            db.execute("INSERT INTO tasks(id,owner_id,prompt,agent_limit,max_attempts,token_budget,created_at,updated_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?)",
                       (task_id, owner_id, prompt, agent_limit, max_attempts, token_budget, now(), now(), idempotency_key))
            db.execute('INSERT INTO resource_policies VALUES(?,?,?,?,?,?)', (task_id, allocation_mode, ceiling_agents, ceiling_tokens, json.dumps(assessment), fingerprint))
            self._insert_item(db, task_id, "Plan the assignment", "planner", prompt)
            self._event(db, task_id, "created", f"Task queued. Capacity {agent_limit} includes coordinator, workers, and verifier.")
            self._event(db, task_id, 'resource_assessment', f"{allocation_mode.title()} allocation: {assessment['complexity']} task; {token_budget:,} tokens allocated, ceiling {ceiling_tokens:,}; agent ceiling {ceiling_agents}.")
            return self._task(db, task_id)

    @staticmethod
    def _item(row, internal=False):
        item = dict(row)
        item["dependencies"] = json.loads(item["dependencies"])
        if not internal:
            for key in ("prompt", "lease_token", "lease_until", "response", "usage_recorded", "reserved_tokens", "available_at"):
                item.pop(key, None)
        return item

    def detail(self, task_id, owner_id=None):
        with self.lock:
            task = self._task(self.db, task_id, owner_id)
            if not task:
                return None
            return {"task": task,
                    "items": [self._item(r) for r in self.db.execute("SELECT * FROM items WHERE task_id=? ORDER BY created_at,id", (task_id,))],
                    "events": [dict(r) for r in self.db.execute("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,))],
                    "handoffs": [dict(r) for r in self.db.execute("SELECT id,source_item_id,target_item_id,message,created_at FROM handoffs WHERE task_id=? ORDER BY id", (task_id,))],
                    "artifacts": [dict(r) for r in self.db.execute("SELECT id,task_id,name,media_type,size,created_at FROM artifacts WHERE task_id=? ORDER BY created_at", (task_id,))]}

    def get_artifact(self, task_id, artifact_id, owner_id=None):
        with self.lock:
            if not self._task(self.db, task_id, owner_id):
                return None
            row = self.db.execute("SELECT * FROM artifacts WHERE task_id=? AND id=?", (task_id, artifact_id)).fetchone()
            return dict(row) if row else None

    def control(self, task_id, action, owner_id=None):
        with self.tx() as db:
            task = self._task(db, task_id, owner_id)
            if not task:
                return None
            if action not in {"pause", "resume", "cancel"}:
                raise ValueError("Unknown task action")
            if action == "cancel" and task["status"] not in {"completed", "cancelled"}:
                self._hold_interrupted(db, task_id)
                db.execute("UPDATE tasks SET status='cancelled',updated_at=? WHERE id=?", (now(), task_id))
                db.execute("UPDATE items SET status='cancelled',lease_token=NULL,lease_until=NULL,reserved_tokens=0 WHERE task_id=? AND status IN ('running','queued')", (task_id,))
            elif action == "pause" and task["status"] in {"queued", "running"}:
                db.execute("UPDATE tasks SET status='paused',updated_at=? WHERE id=?", (now(), task_id))
            elif action == "resume" and task["status"] == "paused":
                db.execute("UPDATE tasks SET status='queued',updated_at=? WHERE id=?", (now(), task_id))
            else:
                raise ValueError(f"Cannot {action} a {task['status']} task")
            self._event(db, task_id, action, "Pause requested; in-flight assignments may finish, no new assignments start." if action == "pause" else f"Task {action} requested.")
            return self._task(db, task_id)

    def increase_budget(self, task_id, token_budget, owner_id=None):
        """Resume a budget stop without discarding paid work or retry history.

        The service must also ensure its local provider executions have drained
        before calling this method; invalidated leases alone cannot prove that.
        """
        with self.tx() as db:
            task = self._task(db, task_id, owner_id)
            if not task:
                return None
            if type(token_budget) is not int or not 1000 <= token_budget <= 1000000:
                raise ValueError("Token budget must be an integer from 1,000 to 1,000,000.")
            if task["status"] != "budget_exhausted":
                raise ValueError("Only a budget-exhausted task can receive a budget increase.")
            if token_budget <= task["token_budget"]:
                raise ValueError("New token budget must exceed the current budget.")
            if token_budget <= task["tokens_used"] + task["tokens_uncertain"]:
                raise ValueError("New token budget must cover confirmed and uncertain usage with capacity remaining.")
            rows = db.execute("SELECT * FROM items WHERE task_id=? AND status!='completed'", (task_id,)).fetchall()
            if not rows or any(r["status"] not in {"failed", "cancelled", "queued"} for r in rows):
                raise ValueError("Task has no safely resumable assignments.")
            if any(r["status"] == "failed" and "token budget" not in (r["error"] or "").lower() for r in rows):
                raise ValueError("Task contains a failure unrelated to its token budget.")
            if any(not r["response"] and r["attempt"] >= task["max_attempts"] for r in rows):
                raise ValueError("An unfinished assignment exhausted its retry limit.")
            # Preserve response checkpoints and attempts. A fresh execution gets
            # a new attempt/thread; a paid checkpoint can publish without a call.
            db.execute("UPDATE items SET status='queued',error=NULL,available_at=0,lease_token=NULL,lease_until=NULL,reserved_tokens=0 WHERE task_id=? AND status!='completed'", (task_id,))
            db.execute("UPDATE tasks SET token_budget=?,status='queued',blocker=NULL,updated_at=? WHERE id=?", (token_budget, now(), task_id))
            db.execute('UPDATE resource_policies SET token_ceiling=MAX(token_ceiling,?) WHERE task_id=?',(token_budget,task_id))
            self._event(db, task_id, "budget_increased", f"Owner increased token budget from {task['token_budget']} to {token_budget}; resumed {len(rows)} unfinished assignments. Completed work and usage retained.", agent="owner")
            return self._task(db, task_id)

    def steer(self, task_id, message, owner_id=None):
        if not message.strip() or len(message) > 10000:
            raise ValueError("Steering must contain 1–10,000 characters.")
        with self.tx() as db:
            task = self._task(db, task_id, owner_id)
            if not task:
                return None
            if task["status"] in TERMINAL:
                raise ValueError("Create a new task to continue a finished or blocked run.")
            self._event(db, task_id, "steering", message, agent="owner")
            db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now(), task_id))
            return self._task(db, task_id)

    def recover_expired(self):
        with self.tx() as db:
            rows = db.execute("SELECT * FROM items WHERE status='running' AND lease_until<?", (time.time(),)).fetchall()
            for row in rows:
                task = self._task(db, row["task_id"])
                status = "cancelled" if task["status"] in TERMINAL else "queued"
                if not row["response"] and row["reserved_tokens"]:
                    self._hold_lease(db,row)
                    self._event(db, row["task_id"], "usage_uncertain", f"Interrupted provider request: {row['reserved_tokens']} reserved tokens remain charged against the task budget until billing can be confirmed.", row["id"])
                db.execute("UPDATE items SET status=?,lease_token=NULL,lease_until=NULL,reserved_tokens=0 WHERE id=?", (status, row["id"]))
                self._event(db, row["task_id"], "recovered", "Expired execution lease recovered from durable checkpoint.", row["id"])
            return len(rows)

    def claim(self, engine_id, global_limit=8, lease_seconds=120):
        with self.tx() as db:
            self._rebalance(db, global_limit)
            if db.execute("SELECT COUNT(*) FROM items WHERE status='running'").fetchone()[0] >= global_limit:
                return None
            for taskrow in db.execute("SELECT * FROM tasks WHERE status IN ('queued','running') ORDER BY updated_at").fetchall():
                task = dict(taskrow)
                active = db.execute("SELECT COUNT(*),COALESCE(SUM(reserved_tokens),0) FROM items WHERE task_id=? AND status='running'", (task["id"],)).fetchone()
                if active[0] >= task["agent_limit"]:
                    continue
                remaining = task["token_budget"] - task["tokens_used"] - task["tokens_uncertain"] - active[1]
                rows = db.execute("SELECT * FROM items WHERE task_id=? AND status='queued' AND available_at<=? ORDER BY CASE WHEN response IS NOT NULL THEN 0 ELSE 1 END,created_at,id", (task["id"], time.time())).fetchall()
                for row in rows:
                    deps = json.loads(row["dependencies"])
                    if any(db.execute("SELECT status FROM items WHERE id=? AND task_id=?", (dep, task["id"])).fetchone()[0] != "completed" for dep in deps):
                        continue
                    # Publishing a paid, saved response requires no token budget.
                    if not row["response"] and remaining <= 0:
                        if active[0] == 0:
                            self._stop(db, task["id"], "budget_exhausted", "Token budget reached (including unconfirmed interrupted requests). Increase this task's budget to continue.")
                            break
                        continue
                    # Do not start a tiny allocation while siblings hold funds.
                    if not row['response'] and active[0] and remaining < 8192:
                        continue
                    # A response checkpoint resumes without another provider request/attempt.
                    attempt = row["attempt"] + (0 if row["response"] else 1)
                    if attempt > task["max_attempts"]:
                        self._stop(db, task["id"], "blocked", "Assignment exhausted its retry limit.")
                        break
                    token = uid()
                    reserved = 0 if row["response"] else min(remaining, 20000, max(8192, remaining // max(1, task["agent_limit"] - active[0])))
                    db.execute('INSERT INTO lease_reservations(lease_token,task_id,tokens) VALUES(?,?,?)',(token,task['id'],reserved))
                    db.execute("UPDATE items SET status='running',assigned_agent=?,attempt=?,lease_token=?,lease_until=?,reserved_tokens=? WHERE id=?",
                               (f"{row['role']}-{row['id'][:6]}", attempt, token, time.time() + lease_seconds, reserved, row["id"]))
                    db.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?", (now(), task["id"]))
                    self._event(db, task["id"], "assigned", f"{row['title']} · attempt {attempt}", row["id"], f"{row['role']}-{row['id'][:6]}")
                    item = self._item(db.execute("SELECT * FROM items WHERE id=?", (row["id"],)).fetchone(), True)
                    item["engine_id"] = engine_id
                    item["token_budget_remaining"] = reserved
                    return item
            return None

    def heartbeat(self, item_id, token, lease_seconds=120):
        with self.tx() as db:
            return bool(db.execute("UPDATE items SET lease_until=? WHERE id=? AND lease_token=? AND status='running'", (time.time()+lease_seconds, item_id, token)).rowcount)

    def extend_reservation(self, item, required_total):
        """Grow one live assignment's allowance only from unreserved task funds."""
        with self.tx() as db:
            row = db.execute("SELECT * FROM items WHERE id=? AND lease_token=? AND status='running'", (item["id"], item["lease_token"])).fetchone()
            if not row:
                return 0
            task = self._task(db, item["task_id"])
            if task["status"] not in {"running", "paused"}:
                return 0
            total_reserved = db.execute("SELECT COALESCE(SUM(reserved_tokens),0) FROM items WHERE task_id=? AND status='running'", (item["task_id"],)).fetchone()[0]
            required = task['tokens_used'] + task['tokens_uncertain'] + total_reserved + max(0, int(required_total)-row['reserved_tokens'])
            task['token_budget'] = self._grow_budget(db, task, required, 'Additional context or tool work')
            available = max(0, task["token_budget"] - task["tokens_used"] - task["tokens_uncertain"] - total_reserved)
            allocation = min(max(row["reserved_tokens"], int(required_total)), row["reserved_tokens"] + available)
            if allocation != row["reserved_tokens"]:
                db.execute("UPDATE items SET reserved_tokens=? WHERE id=?", (allocation, item["id"]))
                db.execute('UPDATE lease_reservations SET tokens=? WHERE lease_token=?',(allocation,item['lease_token']))
                self._event(db, item["task_id"], "budget_allocation", f"Extended assignment allowance to {allocation} tokens using unreserved task budget.", item["id"])
            item["reserved_tokens"] = allocation
            return allocation

    def context(self, item):
        with self.lock:
            task_id = item["task_id"]
            is_verifier = item["role"] == "verifier"
            handoff_rows = self.db.execute("SELECT source_item_id,target_item_id,message FROM handoffs WHERE task_id=?" + ("" if is_verifier else " AND (target_item_id=? OR target_item_id IS NULL)"), (task_id,) if is_verifier else (task_id, item["id"])).fetchall()
            handoffs = [dict(r) for r in handoff_rows]
            if is_verifier:
                handoffs = [{**h, "message": h["message"][:500], "message_length": len(h["message"]), "truncated": len(h["message"]) > 500} for h in handoffs]
            context = {"task_id": task_id, "item_id": item["id"], "attempt": item["attempt"], "prompt": self._task(self.db, task_id)["prompt"],
                    "token_budget_remaining": item["token_budget_remaining"],
                    "assignments": [dict(r) for r in self.db.execute("SELECT id,title,role,status FROM items WHERE task_id=?", (task_id,))],
                    "dependencies": [dict(self.db.execute("SELECT id,title,output FROM items WHERE id=?", (dep,)).fetchone()) for dep in item["dependencies"]],
                    "handoffs": handoffs,
                    "steering": [r[0] for r in self.db.execute("SELECT message FROM events WHERE task_id=? AND kind='steering' ORDER BY id", (task_id,))],
                    "artifacts": [dict(r) for r in self.db.execute("SELECT id,item_id,name,media_type,content FROM artifacts WHERE task_id=? AND name NOT LIKE '%-work.md'", (task_id,))]}
            if is_verifier:
                # Operational proof is supplied by the coordinator's actual
                # committed ledger, not inferred from a worker's prose output.
                evidence = []
                for row in self.db.execute("SELECT id,title,role,status,attempt FROM items WHERE task_id=? ORDER BY created_at,id", (task_id,)).fetchall():
                    tools = {r[0].removeprefix("Executed ").removesuffix("."): r[1] for r in self.db.execute("SELECT message,COUNT(*) FROM events WHERE task_id=? AND item_id=? AND kind='tool' GROUP BY message", (task_id, row["id"]))}
                    evidence.append({**dict(row), "executed_tool_counts": tools,
                                     "committed_handoff_targets": sorted({h["target_item_id"] for h in handoffs if h["source_item_id"] == row["id"] and h["target_item_id"]}),
                                     "artifact_ids": [r[0] for r in self.db.execute("SELECT id FROM artifacts WHERE task_id=? AND item_id=? AND name NOT LIKE '%-work.md'", (task_id, row["id"]))]})
                context["execution_evidence"] = evidence
            return context

    def checkpoint_response(self, item, response):
        with self.tx() as db:
            row = db.execute("SELECT * FROM items WHERE id=?", (item["id"],)).fetchone()
            # Record billed usage even if cancelled during an in-flight request.
            usage = max(0, int(response.get("usage", {}).get("total_tokens", 0)))
            cost = max(0, float(response.get("estimated_cost_usd", 0)))
            receipt=db.execute('SELECT * FROM usage_receipts WHERE lease_token=?',(item['lease_token'],)).fetchone()
            lease=db.execute('SELECT * FROM lease_reservations WHERE lease_token=?',(item['lease_token'],)).fetchone()
            previous_uncertain=lease['uncertain'] if lease else 0
            if receipt is None or previous_uncertain:
                previous_usage=receipt['tokens'] if receipt else 0
                previous_cost=receipt['estimated_cost_usd'] if receipt else 0
                usage=max(usage,previous_usage);cost=max(cost,previous_cost)
                allowance=lease['tokens'] if lease else item.get('reserved_tokens',0)
                uncertain=max(0,allowance-usage) if response.get('usage_uncertain') else 0
                db.execute('INSERT INTO usage_receipts(lease_token,task_id,tokens,estimated_cost_usd) VALUES(?,?,?,?) ON CONFLICT(lease_token) DO UPDATE SET tokens=excluded.tokens,estimated_cost_usd=excluded.estimated_cost_usd',(item['lease_token'],item['task_id'],usage,cost))
                db.execute('UPDATE lease_reservations SET uncertain=? WHERE lease_token=?',(uncertain,item['lease_token']))
                db.execute("UPDATE tasks SET tokens_used=tokens_used+?,tokens_uncertain=MAX(0,tokens_uncertain+?),estimated_cost_usd=estimated_cost_usd+?,updated_at=? WHERE id=?", (usage-previous_usage, uncertain-previous_uncertain, cost-previous_cost, now(), item["task_id"]))
                db.execute("UPDATE items SET usage_recorded=1,reserved_tokens=0 WHERE id=? AND lease_token=?", (item["id"],item['lease_token']))
                if response.get("usage_uncertain"):
                    self._event(db, item["task_id"], "usage_uncertain", f"Request stopped with {usage} confirmed tokens; up to {uncertain} additional reserved tokens have unconfirmed billing.", item["id"])
            if row["lease_token"] != item["lease_token"] or row["status"] != "running":
                return False
            db.execute("UPDATE items SET response=? WHERE id=?", (json.dumps(response), item["id"]))
            return True

    def _stop(self, db, task_id, status, reason):
        self._hold_interrupted(db,task_id)
        db.execute("UPDATE tasks SET status=?,blocker=?,updated_at=? WHERE id=?", (status, reason, now(), task_id))
        db.execute("UPDATE items SET status='cancelled',lease_token=NULL,lease_until=NULL,reserved_tokens=0 WHERE task_id=? AND status IN ('queued','running')", (task_id,))
        self._event(db, task_id, status, reason)

    def _hold_lease(self,db,row):
        if not row['lease_token'] or row['response'] or not row['reserved_tokens']:
            return
        if db.execute('SELECT 1 FROM usage_receipts WHERE lease_token=?',(row['lease_token'],)).fetchone():
            return
        lease=db.execute('SELECT uncertain FROM lease_reservations WHERE lease_token=?',(row['lease_token'],)).fetchone()
        previous=lease[0] if lease else 0
        amount=max(0,row['reserved_tokens']-previous)
        db.execute('INSERT INTO lease_reservations(lease_token,task_id,tokens,uncertain) VALUES(?,?,?,?) ON CONFLICT(lease_token) DO UPDATE SET uncertain=excluded.uncertain',(row['lease_token'],row['task_id'],row['reserved_tokens'],row['reserved_tokens']))
        db.execute('UPDATE tasks SET tokens_uncertain=tokens_uncertain+? WHERE id=?',(amount,row['task_id']))

    def _hold_interrupted(self,db,task_id):
        for row in db.execute("SELECT * FROM items WHERE task_id=? AND status='running'",(task_id,)).fetchall():
            self._hold_lease(db,row)

    def fail(self, item, message, recoverable=True, delay=None, terminal_status="blocked"):
        with self.tx() as db:
            row = db.execute("SELECT * FROM items WHERE id=? AND lease_token=? AND status='running'", (item["id"], item["lease_token"])).fetchone()
            if not row:
                return
            # A checkpoint write may have failed after a paid provider response.
            # Hold its durable reservation before any retry can spend the funds.
            self._hold_lease(db,row)
            task = self._task(db, item["task_id"])
            retry = recoverable and row["attempt"] < task["max_attempts"]
            db.execute("UPDATE items SET status=?,error=?,lease_token=NULL,lease_until=NULL,response=NULL,usage_recorded=0,reserved_tokens=0,available_at=? WHERE id=?",
                       ("queued" if retry else "failed", str(message)[:2000], time.time()+(delay if delay is not None else min(60, 2**row["attempt"])), item["id"]))
            self._event(db, item["task_id"], "retry" if retry else "failed", str(message), item["id"], item["assigned_agent"])
            if not retry:
                self._stop(db, item["task_id"], terminal_status, f"{item['title']}: {message}")

    def defer_budget_contention(self,item):
        """Retry only when sibling settlement could release the needed funds."""
        with self.tx() as db:
            siblings=db.execute("SELECT COALESCE(SUM(reserved_tokens),0) FROM items WHERE task_id=? AND id!=? AND status='running'",(item['task_id'],item['id'])).fetchone()[0]
            if not siblings:
                return False
            receipt=db.execute('SELECT tokens FROM usage_receipts WHERE lease_token=?',(item['lease_token'],)).fetchone()
            paid=bool(receipt and receipt[0])
            task=self._task(db,item['task_id'])
            if paid and item['attempt']>=task['max_attempts']:
                self._stop(db,item['task_id'],'blocked','Assignment reached its paid-attempt limit while waiting for token reservations. No further paid retry was admitted.')
                return True
            changed=db.execute("UPDATE items SET status='queued',attempt=MAX(0,attempt-?),lease_token=NULL,lease_until=NULL,reserved_tokens=0,response=NULL,usage_recorded=0,available_at=? WHERE id=? AND lease_token=? AND status='running'",(0 if paid else 1,time.time()+2,item['id'],item['lease_token'])).rowcount
            if changed:self._event(db,item['task_id'],'resource_wait','Waiting for sibling token reservations to settle before retrying this assignment.',item['id'])
            return bool(changed)

    def retry_publication(self,item):
        """A durable paid answer survives a transient publication/storage error."""
        with self.tx() as db:
            row=db.execute("SELECT response FROM items WHERE id=? AND lease_token=? AND status='running'",(item['id'],item['lease_token'])).fetchone()
            if not row or not row[0] or not json.loads(row[0]).get('content'):
                return False
            db.execute("UPDATE items SET status='queued',lease_token=NULL,lease_until=NULL,reserved_tokens=0,available_at=? WHERE id=?",(time.time()+2,item['id']))
            self._event(db,item['task_id'],'publication_retry','Saved paid response retained; publication will retry without another model request.',item['id'])
            return True

    def interrupt_for_restart(self, item):
        """Requeue a shutdown interruption after its known usage was recorded."""
        with self.tx() as db:
            changed = db.execute("UPDATE items SET status='queued',lease_token=NULL,lease_until=NULL,reserved_tokens=0,response=NULL,usage_recorded=0,available_at=0,error='Worker stopped; retry scheduled after restart.' WHERE id=? AND lease_token=? AND status='running'", (item["id"], item["lease_token"])).rowcount
            if changed:
                db.execute("UPDATE tasks SET status=CASE WHEN status='paused' THEN status ELSE 'queued' END,updated_at=? WHERE id=? AND status IN ('running','queued','paused')", (now(), item["task_id"]))
                self._event(db, item["task_id"], "recovered", "Worker shutdown preserved accounting and queued the interrupted assignment for restart.", item["id"])
            return bool(changed)

    def _artifact(self, db, task_id, item_id, name, content, media_type="text/markdown"):
        name = Path(str(name)).name[:180] or "artifact.txt"
        artifact_id = hashlib.sha256(f"{task_id}:{item_id}:{name}".encode()).hexdigest()[:32]
        content = str(content)[:2000000]
        db.execute("INSERT INTO artifacts(id,task_id,item_id,name,media_type,size,content,created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(task_id,item_id,name) DO UPDATE SET content=excluded.content,size=excluded.size",
                   (artifact_id, task_id, item_id, name, media_type[:100], len(content.encode()), content, now()))

    def complete(self, item, response, plan=None, verification=None):
        with self.tx() as db:
            row = db.execute("SELECT * FROM items WHERE id=? AND lease_token=? AND status='running'", (item["id"], item["lease_token"])).fetchone()
            if not row:
                return False
            task = self._task(db, item["task_id"])
            db.execute("UPDATE items SET status='completed',output=?,error=NULL,lease_token=NULL,lease_until=NULL,reserved_tokens=0 WHERE id=?", (response["content"], item["id"]))
            for artifact in response.get("artifacts", []):
                self._artifact(db, item["task_id"], item["id"], artifact["name"], artifact["content"], artifact.get("media_type", "text/plain"))
            for handoff in response.get("handoffs", []):
                target = handoff.get("target_item_id")
                if target and not db.execute("SELECT 1 FROM items WHERE id=? AND task_id=?", (target, item["task_id"])).fetchone():
                    continue
                db.execute("INSERT INTO handoffs(task_id,source_item_id,target_item_id,message,created_at) VALUES(?,?,?,?,?)", (item["task_id"], item["id"], target, str(handoff["message"])[:10000], now()))
                self._event(db, item["task_id"], "handoff", handoff["message"], target, item["assigned_agent"])
            for event in response.get("tool_events", []):
                self._event(db, item["task_id"], event.get("kind", "tool"), event.get("message", "Tool completed"), item["id"], item["assigned_agent"])
            self._event(db, item["task_id"], "completed", f"{item['title']} completed; output saved.", item["id"], item["assigned_agent"])
            if plan is not None:
                policy = db.execute('SELECT * FROM resource_policies WHERE task_id=?', (item['task_id'],)).fetchone()
                if policy and policy['mode'] == 'auto':
                    assessment = {**json.loads(policy['assessment']), **plan_assessment(plan)}
                    db.execute('UPDATE resource_policies SET assessment=? WHERE task_id=?', (json.dumps(assessment), item['task_id']))
                    self._grow_budget(db, task, assessment['recommended_tokens'], 'Planner assessed the dependency graph')
                    self._event(db, item['task_id'], 'resource_assessment', '; '.join(assessment['reasons']))
                ids = {part["id"]: uid() for part in plan}
                for part in plan:
                    self._insert_item(db, item["task_id"], part["title"], "worker", part["prompt"], [ids[d] for d in part["dependencies"]], ids[part["id"]])
                self._insert_item(db, item["task_id"], "Independently verify deliverables", "verifier", task["prompt"], list(ids.values()))
                self._event(db, item["task_id"], "plan", f"Saved dependency plan with {len(plan)} worker assignments and an independent verifier.")
            elif verification is not None:
                if verification["accepted"]:
                    result = verification["result"]
                    self._artifact(db, item["task_id"], item["id"], "result.md", result)
                    db.execute("UPDATE tasks SET status='completed',result=?,blocker=NULL,updated_at=? WHERE id=?", (result, now(), item["task_id"]))
                    self._event(db, item["task_id"], "verified", "Independent verification accepted the saved deliverable.", item["id"], item["assigned_agent"])
                elif task["repair_round"] + 1 >= task["max_attempts"]:
                    self._stop(db, item["task_id"], "blocked", "Independent verification still found gaps after the repair limit: " + "; ".join(verification["issues"])[:2000])
                else:
                    issues = "\n".join(verification["issues"])
                    repair = self._insert_item(db, item["task_id"], "Repair verification gaps", "worker", "Repair the deliverable based on these verifier findings:\n" + issues, [item["id"], *item["dependencies"]])
                    self._insert_item(db, item["task_id"], "Verify repaired deliverable", "verifier", task["prompt"], [repair])
                    db.execute("UPDATE tasks SET repair_round=repair_round+1 WHERE id=?", (item["task_id"],))
                    db.execute("INSERT INTO handoffs(task_id,source_item_id,target_item_id,message,created_at) VALUES(?,?,?,?,?)", (item["task_id"], item["id"], repair, issues, now()))
                    self._event(db, item["task_id"], "repair", "Verifier rejected incomplete output and assigned a repair: " + issues, repair, item["assigned_agent"])
            elif item["role"] == "worker":
                self._artifact(db, item["task_id"], item["id"], f"{item['id'][:8]}-work.md", response["content"])
            db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now(), item["task_id"]))
            return True

    def _grow_budget(self, db, task, required, reason):
        policy = db.execute('SELECT * FROM resource_policies WHERE task_id=?', (task['id'],)).fetchone()
        current = task['token_budget']
        if not policy or policy['mode'] != 'auto':
            return current
        allocation = growth(current, required, policy['token_ceiling'])
        if allocation > current:
            db.execute('UPDATE tasks SET token_budget=?,updated_at=? WHERE id=?', (allocation, now(), task['id']))
            self._event(db, task['id'], 'resource_budget', f'{reason}: token allocation {current:,} → {allocation:,}; ceiling {policy["token_ceiling"]:,}.')
        return allocation

    def _rebalance(self, db, global_limit):
        for row in db.execute("SELECT t.* FROM tasks t JOIN resource_policies p ON p.task_id=t.id WHERE p.mode='auto' AND t.status IN ('queued','running')").fetchall():
            task = dict(row)
            policy = db.execute('SELECT * FROM resource_policies WHERE task_id=?', (task['id'],)).fetchone()
            items = db.execute('SELECT * FROM items WHERE task_id=?', (task['id'],)).fetchall()
            completed = {i['id'] for i in items if i['status'] == 'completed'}
            active = [i for i in items if i['status'] == 'running']
            ready = [i for i in items if i['status'] == 'queued' and i['available_at'] <= time.time() and set(json.loads(i['dependencies'])) <= completed]
            target = max(1, min(policy['agent_ceiling'], global_limit, len(active)+len(ready)))
            if target != task['agent_limit']:
                db.execute('UPDATE tasks SET agent_limit=? WHERE id=?', (target,task['id']))
                self._event(db,task['id'],'resource_scale',f"Team capacity {task['agent_limit']} → {target}: {len(ready)} ready, {len(active)} active assignments; ceiling {policy['agent_ceiling']}.")
            if ready:
                committed = task['tokens_used']+task['tokens_uncertain']+sum(i['reserved_tokens'] for i in active)
                self._grow_budget(db, task, committed+min(len(ready),target)*20000, 'Ready work requires more allowance')

    def configure_resources(self, task_id, mode, agent_ceiling, token_ceiling, owner_id=None):
        if mode not in {'auto','manual'} or not 1 <= agent_ceiling <= 20 or not 1000 <= token_ceiling <= 1000000:
            raise ValueError('Invalid resource configuration')
        with self.tx() as db:
            task=self._task(db,task_id,owner_id)
            if not task:
                return None
            if task['status'] in TERMINAL:
                raise ValueError('Only an unfinished mission can be reconfigured.')
            reserved=db.execute("SELECT COALESCE(SUM(reserved_tokens),0) FROM items WHERE task_id=? AND status='running'",(task_id,)).fetchone()[0]
            if token_ceiling < task['tokens_used']+task['tokens_uncertain']+reserved:
                raise ValueError('The token ceiling cannot be below used and currently reserved tokens.')
            db.execute('UPDATE resource_policies SET mode=?,agent_ceiling=?,token_ceiling=? WHERE task_id=?',(mode,agent_ceiling,token_ceiling,task_id))
            allocation=token_ceiling if mode=='manual' else min(task['token_budget'],token_ceiling)
            capacity=agent_ceiling if mode=='manual' else min(task['agent_limit'],agent_ceiling)
            db.execute('UPDATE tasks SET agent_limit=?,token_budget=?,updated_at=? WHERE id=?',(capacity,allocation,now(),task_id))
            self._event(db,task_id,'resource_configured',f'Owner selected {mode} allocation, agent ceiling {agent_ceiling}, token ceiling {token_ceiling:,}. In-flight work may finish before a lower agent limit takes effect.',agent='owner')
            return self._task(db,task_id)
