# Phase 3 handoff: workflows and operations

This folder records what the Cloud implementation of the [phase 3 plan](../../docs/plan/end-to-end/3-workflows-and-operations.md) built and checked. There is no login (owner decision, reaffirmed 2026-10-01; the plan, contracts, overview and phase 1/4/5 documents were updated to match). All evidence here is **synthetic**: the fake provider, the four-document test fixture corpus and typed visitor names. No paid call was made, no original document or local runtime was used, and $0 was spent.

The authoritative `.runtime/releases/phase-3/report.md` must be produced on the owner host with `report --phase 3` once that host's own checks have run. The [runbook](../../docs/operations/runbook.md) gives the operating procedure.

## What changed

| Area | Files | Summary |
| --- | --- | --- |
| Access | `auth.py`, `ui.py` | No login. The sidebar name (default `owner`) attributes requests, attempts, reviews, corrections and audit events; the visitor gets every page. Service functions keep their capability checks as role declarations, so narrower in-process principals (CLI, tests) are still refused. |
| Background requests | `service.py` (`RequestRunner`, `submit_answer`, `request_status`, `cancel_request`, `recover_requests`) | Bounded executor: 6 workers, 12 admitted. Persisted input snapshot plus idempotency key, exactly-once `queued → running` claim, and a cancellation checkpoint before each paid stage. Controlled shutdown and conservative restart recovery; nothing is replayed. |
| Modes | `service.py`, `generation.py` | `metadata`: free typed CSV values with known/unknown/0원/conflict/resolved states. `inventory`: free structured requirement rows in source order with a completeness declaration. `compare`: two scoped subqueries with half budgets and redistribution under the 5,000-token ceiling; an answer that drops a side is a technical error. Prompt `grounded-answer-3`. |
| Budget operations | `budget.py`, `service.py` | Snapshot cap warnings (50/75/90 %, exhausted), pacing over the project dates, unknown amount and read time. Reconciliation refuses a changed total under the same ID and accepts only covered `unknown` attempts dispatched inside the interval. Owner settlement from evidence, external adjustments and paid on/off, all audited. A failed settlement becomes `unknown` instead of hanging in `dispatching`. |
| Verifier | `service.py` | Frozen verifier runs (`vr-…`) under versioned configurations (`vc-…`) and run comparison. Append-only corrections that require a verbatim quote from the cited original element. Sealed rows are never served to the page. Redacted request and run exports (no keys or local paths). |
| Sources | `service.py` | Downloads accept only managed IDs of the current version, recheck the file hash and refuse paths outside the originals directory. Evidence views carry review, OCR and revision warnings and fall back to the preserved extraction artifact. |
| UI | `ui.py`, `DESIGN.md` | Visitor name; result cards with wrapping titles and conflict notes; selection of up to two documents; mode choice; request ownership through `target_key`/`may_attach`; read-only 1 s request polling and 2 s budget polling; adjacent evidence panel; request history; verifier trace, comparison and corrections; budget administration page. |
| Ops | `ops.py`, `cli.py` | `check --phase 3`, `load-check`, `reconcile`, `unresolved`, `report --phase 3`. |
| Tests | `tests/test_service.py` | 37 phase-3 tests, plus a shared Kiwi analyzer and weakly registered shutdown, which cut the full suite from ~90 s to ~24 s. |

Schema version 5 adds `audit_events`, `corrections`, `verifier_runs` and three `requests` columns (`request_json`, `mode`, `cancel_requested`). The migration adds them in place, and existing ledger rows are untouched. `EVAL_VERSION` is unchanged (`retrieval-eval-4`): frozen retrieval runs are still valid.

## Commands run here and their outcomes

| Command | Outcome |
| --- | --- |
| `check --phase 3 --provider fake` | 58 tests passed (~13 s) |
| `check --phase 2 --provider fake` (whole suite) | 175 tests passed |
| `load-check --users 6 --provider fake` | Passed. 12 requests at once against a cap admitting 4: 4 answered, 8 refused before dispatch. The cap was never exceeded; budget polling p95 6.6 ms during in-flight calls. See `fake-checks/load-check.json`. |
| `load-check --users 6 --provider fake --fail-every 3` | Passed. Injected post-dispatch timeouts stayed `unknown` with their reserves held, and nothing was retried. See `fake-checks/load-check-fail-every-3.json`. |
| Browser walkthrough (`browser-scripts/`, no login) | 15 of 16 steps passed. Keyboard and contrast checks are only partial. See `browser-results.json` and `screenshots/`. |

The phase-3 scenario table maps to tests in `tests/test_service.py`:

| Scenario | Tests |
| --- | --- |
| Near-cap reservations | `SixUserBudgetTest.test_six_users_near_cap_admit_only_affordable_dispatches` |
| Duplicate submissions | `SubmissionTest` |
| Scope change during a request | `ScopeOwnershipTest` |
| Queued versus running cancellation | `QueueTest`, `CancellationTest` |
| Role declarations and the open visitor | `AuthorizationTest`, `VisitorTest` |
| Billed retry | `test_billed_retry_is_a_new_attempt_and_the_old_cost_stays` |
| Crash and duplicate completion | `ShutdownRestartTest` |
| Double import and late usage | `ReconciliationTest` |
| Ledger down, unknown price, overrun | `test_unavailable_ledger_…`, `test_unknown_price_frozen_ledger_and_overrun_fail_closed` |
| Exhausted cap and polling | `test_exhausted_cap_keeps_free_routes_and_polling_never_dispatches` |

## Decisions made during implementation (for owner review)

1. No login, everywhere. The owner reaffirmed the phase-1 decision for phase 3. The earlier account, token and session requirement was removed from the phase 3 plan, the shared contracts, the overview and the phase 1/4/5 documents, and from the code. Network reach is the only access control, so the deployment decides who can spend and administer.
2. Changing the screen does not cancel running work. Changing scope, mode or date detaches the old request and cancels it only while it is still queued. A running request finishes and stays in history, so its billing settles.
3. Free modes need no question. Basic information and requirement lists take no question text. Their requests are still persisted for history and export.

## Owner steps on the local host

1. Pull, then run `check --phase 3 --provider fake` and `load-check --users 6 --provider fake --save` on the Windows host. The first command the app runs migrates the existing ledger to schema 5.
2. Decide who can reach the app (runbook §2–3). Without login, anyone on the bound network can spend the allowance and use the admin page. Ask members to type one consistent name.
3. Run the browser walkthrough on the real corpus with six people or browsers, the real `.runtime`, and the real HWP and PDF late-page citations. Record viewport, workflow and outcomes as `.runtime/releases/phase-3/browser-results.json`, using the same shape as this folder's file. Then run `report --phase 3`.
4. Optionally run one bounded paid smoke answer after an explicit estimate. Record it as `.runtime/releases/phase-3/paid-smoke.json`.

## Open items

- The team host and its network exposure have not been configured or verified.
- Warm and cold latency on the team host is unmeasured. Here, a cold fake server showed its first reservation ~5.4 s after submit, and a warm one after 260 ms.
- Keyboard-only navigation and color contrast were not measured. Screen-reader output was not checked.
- In a desktop browser, check the download filename (headless Chromium reported `download`).
- Fixture-only retrieval observation (phase-2 policy, unchanged): a question that restates a one-chunk document's title can drop to a single non-discriminating term and return 근거 부족. Watch for it on the real corpus before changing the query policy, which would require a new `EVAL_VERSION`.
