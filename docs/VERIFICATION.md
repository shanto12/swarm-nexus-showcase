# Verification record

September 14, 2026, Central Time. This is a bounded portfolio demonstration, not a claim of universal production readiness.

| Requirement | Method | Evidence / scope |
|---|---|---|
| Original authenticated deployment | HTTP/API | Netlify app and cloud health respond successfully; provider and authentication configured. |
| Actual agent execution | Production UI plus API reconciliation | Arithmetic mission completed: 5 assignments, 7 saved artifacts, 54,685 reported tokens, verified total 990. Captured in `public/capture/mission.json`. |
| Bounded token policy | Production UI | Mission stopped at its original 60,000-token ceiling; continuation to a 100,000 ceiling retained completed work. Final actual usage was 54,685 tokens. Ceiling is admission capacity, not actual usage. |
| Operator controls | Isolated Playwright Chrome | Login, pause/resume, cancellation-dialog dismissal, budget continuation, five mission tabs, artifact download, logout revocation and login again exercised. |
| Artifact provenance | Production API and browser | Seven artifact response sizes and SHA-256 values reconciled; a real browser download succeeded. Public copies preserve those bytes. |
| Original frontend backend unit tests | Local | 47 Python runtime tests; 14 proxy/security tests passed. |
| Public source build and types | Local | TypeScript and Vite build pass. |
| Dependency audit | npm | Production dependency audit reports zero known vulnerabilities at check time. |
| Public captured UI | Deployed Playwright | Tabs, mission selection, brief disclosure, tool dialogs, refresh, navigation, artifact downloads and desktop/mobile layouts are tested against the public Netlify URL. |
| Public security boundary | Source and HTTP | Captured mode reads local synthetic JSON. No live API function, owner session, credentials, or provider invocation is deployed. `/api/*` is explicitly denied. |
| Original API security headers | HTTP | Eight header observations passed: CSP, frame denial, HSTS, nosniff, referrer policy, permissions policy and no-store API boundaries. |
| Real owner Chrome profile | Separate manual pass | Coordinated by the main portfolio task; this agent's Playwright checks use isolated Chrome, not the owner's saved profile. No saved-password autofill claim is made here. |

## Known operational limits

The public interface is a capture of actual execution, not a public multi-tenant service. It intentionally does not allow new work or state changes. The original backend is a single replica and does not provide multi-host failover or autoscaling.

A second, more complex synthetic launch-readiness mission exposed a blocked synthesis/verification path under concurrent token reservations and a two-attempt limit. Its partial worker artifacts are not presented as a completed final plan. That issue is under separate runtime repair and must be reverified before claiming that path succeeds. The deterministic completed capture remains valid evidence of its own execution.

Initial verification issues were test-harness expectations (the create endpoint correctly returns HTTP 201, and the result tab is named Result). A Netlify-injected badge generated CSP console errors on the new public site; its injection was disabled without weakening the site's CSP. Original authenticated-page navigation produced one cancelled polling request during navigation/logout; this is recorded as an aborted request, not a backend HTTP failure.

The orbit image is 1672 × 941. Desktop screenshots are captured at 1600 × 1100; neither that image nor simulated viewport testing is described as native 4K artwork or a physical-mobile-device test.
