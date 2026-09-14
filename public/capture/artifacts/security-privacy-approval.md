# Security, Privacy & Human Approval (fictional scenario; all facts assumed)

**Injection/jailbreak defenses:** strict instruction hierarchy (system > policy > user > retrieved content); retrieved text treated as data, never instructions; input/output filters for injection and jailbreak patterns; tool allowlist (read-only KB search, ticket lookup) with no arbitrary code or network tools.

**Data privacy:** PII minimization at ingestion; redaction of card/ID numbers before logging; short retention (assumed 30 days) with deletion jobs; role-based access control and audited access.

**Human-approval gates:** refunds, account changes, and data deletion require human confirmation before execution; assistant may only draft.

**Acceptance tests (proposed, not executed):**
1. proposed: 0/50 injection payloads trigger unauthorized tool calls.
2. proposed: 100% of out-of-allowlist tool requests blocked.
3. proposed: 0 PII tokens in 100 sampled logs.
4. proposed: 0/20 refund/account/deletion requests execute without human approval.
5. proposed: 100% of retained records deleted within 24h of the 30-day window.
