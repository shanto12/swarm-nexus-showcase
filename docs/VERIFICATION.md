# Verification record

September 14, 2026, Central Time. This is a bounded portfolio demonstration, not a claim of universal production readiness.

| Requirement | Method | Evidence / scope |
|---|---|---|
| Original authenticated deployment | HTTP/API | Netlify app and cloud health respond successfully; provider and authentication configured. |
| Actual agent execution | Production UI plus API reconciliation | Featured launch-planning mission completed: planner, two independent workers, synthesizer and verifier; 7 saved artifacts; 49,706 reported tokens; zero uncertain usage. Captured in `public/capture/mission.json`. Its proposed acceptance tests were not executed. |
| Bounded token policy | Production UI | Mission stopped at its original 60,000-token ceiling; continuation to a 100,000 ceiling retained completed work. Final actual usage was 54,685 tokens. Ceiling is admission capacity, not actual usage. |
| Operator controls | Isolated Playwright Chrome | Login, pause/resume, cancellation-dialog dismissal, budget continuation, five mission tabs, artifact download, logout revocation and login again exercised. |
| Artifact provenance | Production API and browser | Seven artifact response sizes and SHA-256 values reconciled; a real browser download succeeded. Public copies preserve those bytes. |
| Original frontend backend unit tests | Local | 55 Python runtime tests (including eight reservation-settlement regressions); 14 proxy/security tests passed in the published source checkout. |
| Public source build and types | Local | TypeScript and Vite build pass. |
| Dependency audit | npm | Production dependency audit reports zero known vulnerabilities at check time. |
| Public captured UI | Deployed Playwright | Tabs, mission selection, brief disclosure, tool dialogs, refresh, navigation, artifact downloads and desktop/mobile layouts are tested against the public Netlify URL. |
| Public security boundary | Source and HTTP | Captured mode reads local synthetic JSON. No live API function, owner session, credentials, or provider invocation is deployed. `/api/*` is explicitly denied. |
| Original API security headers | HTTP | Eight header observations passed: CSP, frame denial, HSTS, nosniff, referrer policy, permissions policy and no-store API boundaries. |
| Real owner Chrome profile | Separate manual pass | Coordinated by the main portfolio task; this agent's Playwright checks use isolated Chrome, not the owner's saved profile. No saved-password autofill claim is made here. |

## Known operational limits

The public interface is a capture of actual execution, not a public multi-tenant service. It intentionally does not allow new work or state changes. The original backend is a single replica and does not provide multi-host failover or autoscaling.

A synthetic launch-readiness mission exposed premature paid retries while sibling token reservations remained unsettled. The runtime now persists a reservation-wait marker and waits for sibling settlement before admitting the retry, preserving paid attempts and uncertain accounting. Eight focused regression tests cover contention, missing/uncertain receipts, stale leases and restart behavior. Release `b3d4e9b6a590ddad` is healthy. A new, explicitly scoped enterprise mission completed all five assignments under the same 100,000-token, two-agent, two-attempt controls. The original blocked record and its accounting remain unchanged. The new prompt also explicitly avoids duplicating the runtime’s automatic verifier; this is not claimed as a verbatim replay of the earlier prompt.

Initial verification issues were test-harness expectations (the create endpoint correctly returns HTTP 201, and the result tab is named Result). A Netlify-injected badge generated CSP console errors on the new public site; its injection was disabled without weakening the site's CSP. Original authenticated-page navigation produced one cancelled polling request during navigation/logout; this is recorded as an aborted request, not a backend HTTP failure.

The orbit image is 1672 × 941. Final landscape screenshots are captured at 1440 × 800; neither that image nor simulated viewport testing is described as native 4K artwork or a physical-mobile-device test.

## Featured capture receipts

- Runtime release: `b3d4e9b6a590ddad`; source repair commit: `c56f35cd246bbc34cbca6fd88765ca47ce48772c`.
- Five completed assignments; 49,706 reported tokens; zero uncertain usage; backend estimated cost $0.012131136 (not a provider invoice).
- `launch-plan.md`: 2,504 bytes; SHA-256 `a9ee567d6c2edfabc2d26788eb6528311d9f61ad44f22db7a01433b95a55d63f`.
- Proposed launch checks are planning output. They are not evidence that an actual support system passed those checks.
