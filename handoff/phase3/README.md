# Phase 3 handoff: workflows and operations

This folder records what the Cloud implementation of the [phase 3 plan](../../docs/plan/end-to-end/3-workflows-and-operations.md) built and checked. All evidence here is **synthetic**: the fake provider, the four-document test fixture corpus and throwaway accounts. No paid call was made, no original document or local runtime was used, and **$0 was spent**.

The authoritative `.runtime/releases/phase-3/report.md` must be produced on the owner host with `report --phase 3` once that host's own checks have run. The [runbook](../../docs/operations/runbook.md) gives the operating procedure.

## What changed

| Area | Files | Summary |
| --- | --- | --- |
| Identity | `auth.py`, `settings.py`, `store.py` | Private accounts file (token SHA256 digests, roles, enabled), server sessions (8 h, revocable, invalidated by token or role changes), bounded generic login failures. Every public service call revalidates. Open mode without accounts is kept for the phase-1 owner decision, but it is loopback only and grants neither `budget_admin` nor sealed access. |
| Background requests | `service.py` (`RequestRunner`, `submit_answer`, `request_status`, `cancel_request`, `recover_requests`) | Bounded executor: 6 workers, 12 admitted. Persisted input snapshot plus idempotency key, exactly-once `queued → running` claim, and checkpoints before each paid stage (cancellation, ended session). Controlled shutdown and conservative restart recovery; nothing is replayed. |
| Modes | `service.py`, `generation.py` | `metadata`: free typed CSV values with known/unknown/0원/conflict/resolved states. `inventory`: free structured requirement rows in source order with a completeness declaration. `compare`: two scoped subqueries with half budgets and redistribution under the 5,000-token ceiling; an answer that drops a side is a technical error. Prompt `grounded-answer-3`. |
| Budget operations | `budget.py`, `service.py` | Snapshot cap warnings (50/75/90 %, exhausted), pacing over the project dates, unknown amount and read time. Reconciliation refuses a changed total under the same ID and accepts only covered `unknown` attempts dispatched inside the interval. Owner settlement from evidence, external adjustments and paid on/off, all audited. A failed settlement becomes `unknown` instead of hanging in `dispatching`. |
| Verifier | `service.py` | Frozen verifier runs (`vr-…`) under versioned configurations (`vc-…`) and run comparison. Append-only corrections that require a verbatim quote from the cited original element. Sealed rows are refused. Redacted request and run exports (no sessions, tokens or local paths). |
| Sources | `service.py` | Downloads accept only managed IDs of the current version, recheck the file hash and refuse paths outside the originals directory. Evidence views carry review, OCR and revision warnings and fall back to the preserved extraction artifact. |
| UI | `ui.py`, `DESIGN.md` | Login; result cards with wrapping titles and conflict notes; selection of up to two documents; mode choice; request ownership through `target_key`/`may_attach`; read-only 1 s request polling and 2 s budget polling; adjacent evidence panel; request history; verifier trace, comparison and corrections; budget administration page. |
| Ops | `ops.py`, `cli.py` | `check --phase 3`, `load-check`, `accounts`, `reconcile`, `unresolved`, `report --phase 3`. |
| Tests | `tests/test_service.py` | 43 phase-3 tests, plus a shared Kiwi analyzer and weakly registered shutdown, which cut the full suite from ~90 s to ~24 s. |

Schema version 5 adds `sessions`, `login_failures`, `audit_events`, `corrections`, `verifier_runs` and four `requests` columns. The migration adds them in place, and existing ledger rows are untouched. `EVAL_VERSION` is unchanged (`retrieval-eval-4`): frozen retrieval runs are still valid.

## Commands run here and their outcomes

| Command | Outcome |
| --- | --- |
| `check --phase 3 --provider fake` | 64 tests passed, three consecutive runs (~13 s each) |
| `check --phase 2 --provider fake` (whole suite) | 181 tests passed |
| `load-check --users 6 --provider fake` | Passed. 12 requests at once against a cap admitting 4: 4 answered, 8 refused before dispatch. The cap was never exceeded; budget polling p95 6.6 ms during in-flight calls. See `fake-checks/load-check.json`. |
| `load-check --users 6 --provider fake --fail-every 3` | Passed. Injected post-dispatch timeouts stayed `unknown` with their reserves held, and nothing was retried. See `fake-checks/load-check-fail-every-3.json`. |
| Browser walkthrough (`browser-scripts/`) | 15 of 16 steps passed. Keyboard and contrast checks are only partial. See `browser-results.json` and `screenshots/`. |

The phase-3 scenario table maps to tests in `tests/test_service.py`:

| Scenario | Tests |
| --- | --- |
| Near-cap reservations | `SixUserBudgetTest.test_six_users_near_cap_admit_only_affordable_dispatches` |
| Duplicate submissions | `SubmissionTest` |
| Scope change during a request | `ScopeOwnershipTest` |
| Queued versus running cancellation | `QueueTest`, `CancellationTest` |
| Session ended between stages | `SessionBetweenStagesTest` |
| Direct privileged calls | `AuthorizationTest` |
| Billed retry | `test_billed_retry_is_a_new_attempt_and_the_old_cost_stays` |
| Crash and duplicate completion | `ShutdownRestartTest` |
| Double import and late usage | `ReconciliationTest` |
| Ledger down, unknown price, overrun | `test_unavailable_ledger_…`, `test_unknown_price_frozen_ledger_and_overrun_fail_closed` |
| Exhausted cap and polling | `test_exhausted_cap_keeps_free_routes_and_polling_never_dispatches` |

## Decisions made during implementation (for owner review)

1. **Open mode is kept, but narrowed.** The phase-1 owner decision (no login) conflicts with phase 3 step 1 (owner-provisioned identities). Both now coexist: the presence of the private accounts file selects token mode. Without it, the app runs only on a loopback address, and the typed name gets `consultant` and `verifier` but never `budget_admin` or `sealed_evaluator`. Shared use requires `accounts provision`.
2. **Changing the screen does not cancel running work.** Changing scope, mode or date detaches the old request and cancels it only while it is still queued. A running request finishes and stays in history, so its billing settles. Logging out cancels queued work.
3. **Free modes need no question.** Basic information and requirement lists take no question text. Their requests are still persisted for history and export.
4. **Reload means a new login.** A reload starts a new Streamlit session, so the member logs in again. A session cookie would need a component that sets cookies. The session ID is kept out of URLs.

## Owner steps on the local host

1. Pull, then run `check --phase 3 --provider fake` and `load-check --users 6 --provider fake --save` on the Windows host. The first command the app runs migrates the existing ledger to schema 5.
2. Decide team access (runbook §3). Provision members with `accounts provision` and deliver the tokens privately. Give `budget_admin` only to the owner.
3. Run the browser walkthrough on the real corpus with six people or browsers, the real `.runtime`, and the real HWP and PDF late-page citations. Record viewport, role and outcomes as `.runtime/releases/phase-3/browser-results.json`, using the same shape as this folder's file. Then run `report --phase 3`.
4. Optionally run one bounded paid smoke answer after an explicit estimate. Record it as `.runtime/releases/phase-3/paid-smoke.json`.

## Open items

- Hosting, TLS or tunnel, and token distribution have not been configured or verified.
- Warm and cold latency on the team host is unmeasured. Here, a cold fake server showed its first reservation ~5.4 s after submit, and a warm one after 260 ms.
- Keyboard-only navigation and color contrast were not measured. Screen-reader output was not checked.
- In a desktop browser, check the download filename (headless Chromium reported `download`).
- Fixture-only retrieval observation (phase-2 policy, unchanged): a question that restates a one-chunk document's title can drop to a single non-discriminating term and return 근거 부족. Watch for it on the real corpus before changing the query policy, which would require a new `EVAL_VERSION`.
