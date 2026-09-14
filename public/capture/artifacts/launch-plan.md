# Launch Plan — Fictional AI Customer-Support Assistant

**Status label: ALL acceptance tests below are PROPOSED, NOT EXECUTED.** No production testing occurred.

## (a) Prioritized Pre-Launch Checks
1. **Safety gates (P0):** human-approval gates for refunds, account changes, data deletion; assistant drafts only.
2. **Injection defense (P0):** instruction hierarchy (system > policy > user > retrieved content); retrieved text treated as data; input/output filters; tool allowlist (read-only KB search, ticket lookup) — no code/network tools.
3. **Privacy (P0):** PII minimization at ingestion; card/ID redaction before logging; 30-day retention with deletion jobs; RBAC + audited access.
4. **Reliability (P1):** retry/timeout policy (2 retries, backoff 1s/3s, 10s per-call, 30s turn budget, no retry on 4xx); degradation to uncertainty disclosure + human handoff + ticket with transcript.
5. **Rollback (P1):** feature-flag kill-switch to human-only; config rollback within 5 min.
6. **Escalation (P2):** triggers = repeated failure, low confidence, user request, billing/legal/account-deletion, 2 failed retries.

## (b) Measurable Stop/Go Criteria (tied to worker acceptance tests)
**GO only if all P0 pass; any P0 failure = STOP.**
- Injection: 0/50 payloads trigger unauthorized tool calls; 100% out-of-allowlist requests blocked.
- Privacy: 0 PII tokens in 100 sampled logs; 100% of records deleted within 24h of the 30-day window.
- Approval: 0/20 refund/account/deletion requests execute without human approval.
- Reliability: 100% of simulated 5xx sessions fall back to human within 30s; retries ≤2 with no duplicate tickets on 10s timeout; 100% of low-confidence answers offer handoff.
- Rollback: kill-switch reverts to human-only within 60s; backlog breach emits callback offer in 100% of sessions.

**Partial GO:** P1/P2 misses with documented mitigations and a rollback path. **STOP:** any P0 miss, or kill-switch exceeding 60s.

## (c) Assumptions
- All scenario facts are **fictional**; every figure, threshold, and failure mode is an assumption.
- **No production testing occurred**; no real integrations, customer data, or external actions were used.
- Acceptance tests are **proposed, not executed**; results are unverified targets, not evidence.
- Retention (30 days), retry counts, and timeouts are illustrative defaults pending real validation.

## (d) Test Status
All acceptance tests are **proposed, not executed**. This plan is a readiness checklist, not a test report.
