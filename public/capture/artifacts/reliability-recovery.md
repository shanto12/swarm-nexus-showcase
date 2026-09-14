# Reliability & Recovery (fictional scenario; all facts assumed, no production testing)

**Failure modes:** model/API timeout; hallucinated answer; tool/CRM outage; queue backlog.

**Degradation & fallback:** on timeout or tool error, assistant states uncertainty, offers a human handoff, and opens a ticket with transcript. If confidence is low or CRM is unreachable, it stops answering and routes to a human queue. Backlog above threshold triggers a "longer wait" notice plus callback option.

**Retry/timeout policy:** 2 retries with exponential backoff (1s, 3s), 10s per-call timeout, 30s total turn budget; no retry on 4xx.

**Escalation triggers:** repeated failure, low confidence, user request, billing/legal/account-deletion topics, 2 failed retries.

**Rollback/kill-switch:** feature flag disables the assistant instantly, reverting to human-only support; config rollback within 5 minutes.

**Acceptance tests (proposed, not executed):**
1. proposed: on simulated 5xx, 100% of sessions fall back to human within 30s.
2. proposed: on simulated 10s timeout, retries ≤2 and no duplicate tickets.
3. proposed: 100% of low-confidence answers offer human handoff.
4. proposed: kill-switch reverts to human-only routing within 60s.
5. proposed: backlog threshold breach emits callback offer in 100% of sessions.
