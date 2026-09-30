# Phase 3 — usable workflows and six-user operational correctness

Goal: complete the consultant and verification experiences on one shared pipeline, with responsive allowance status, authorized actions, trustworthy evidence navigation and correct request/billing behavior across six concurrent sessions.

Expected window: day 2. The minimal pages already exist from phase 1. Enter with their actual handoff and the active/fallback retrieval configuration from [phase 2](2-corpus-and-retrieval.md). Dense/reranker availability is not a prerequisite for this phase's core workflows.

## Required reading and files

Read [shared contracts](implementation-contracts.md), [frontend research](../../rag/frontend.md), [budget research](../../rag/budget.md) and both preceding handoffs. Inspect current `app.py`, `ui.py`, `auth.py`, `service.py`, `generation.py`, `budget.py` and `store.py` before editing them. Do not create a second API/frontend stack.

Extend those files and create `tests/test_service.py` for request ownership, authorization and concurrency. Add `DESIGN.md` before substantial layout changes and `docs/operations/runbook.md` for actual deployment/recovery commands. Follow the frontend design skill when implementation begins; this planning task does not choose a palette or claim browser measurements.

## Interface specification

### Consultant work screen

Reading order on a wide screen: compact historical/as-of and allowance strip → project search/filters and readable list → selected-project question/answer → adjacent evidence panel. Narrow screens preserve that order. Keep a single primary task visible; use expanders for provenance and token details.

| Area | Required content and behavior |
| --- | --- |
| Search | Keyword, institution, amount/date filters, unknown/conflict handling; submit does local search only |
| Result | Wrapping project title, actual/recorded institution with provenance warning where needed, KRW amount, publication/closing date, source-review status; never imply currently open by default |
| Selection | Explicit title, source version and as-of date beside the question; clear document-specific state when selection changes |
| Question | Labeled Korean form, bounded text, single-document/explicit two-document comparison mode, disabled duplicate submission while the same request is active |
| Answer | Short conclusion, supported claims, qualifiers/inferences, unknown/conflicting data and next verification action; technical failure appears separately |
| Evidence | Clickable server-generated citation, exact quote and nearby section/header/cells, original download; PDF physical page/region or HWP structural location |
| Budget | Spent dollars/20 percentage, pending reservations, current request charge/billing state, cap warning; expandable token/member/pacing/freshness details |

User-facing text is Korean. Do not put chunk IDs, model names or retrieval scores into the ordinary task flow. Do not show uncalibrated confidence percentages. Empty source data, zero amounts and missing dates need distinct labels.

### Verification entry

Authorize before rendering or loading verifier data. Choose a reviewed dev question or scoped manual query, a versioned config and an explicit action. Retrieval-only is the default. Freeze each result under a run ID; changing controls does not relabel an old run.

Display ingestion/review state → source elements → chunks/analyzer/filter scope → channel ranks/RRF/reranker → expanded/packed evidence and token count → validated answer/claims → token/cost/attempt state. Score labels state their meaning; they are not user confidence.

Provide independent actions for retrieval, estimated generation, label correction, export and controlled comparison. Dense retrieval can incur a query embedding charge: show that estimate/cache status even though answer generation is off. Gold drafting, LLM judging and index builds are additional explicitly costed owner/verifier actions, not automatic side effects of viewing a dataset row.

Ordinary verifier access excludes sealed test questions/labels and budget administration. A dedicated `sealed_evaluator` action runs final evaluation only under the phase-4 freeze procedure. Exports redact keys, token credentials, private identity details and unrestricted local paths. Cross-member traces require verifier authority; consultant requests remain scoped to their session/member.

## Ordered implementation

### 1. Strengthen server identity and authorization

1. Revalidate configured account roles and token hashes. Provision six owner-controlled identities; never let a free-text member selector determine billing authority. Fake users can operate only against the fake transport.
2. Validate session expiry/revocation in public service functions and before each newly dispatched paid stage. An already dispatched call still settles after logout/revocation; authorization does not erase its charge.
3. Restrict verifier, budget-admin, source download and sealed-data actions separately. Validate permission on cached reads as well as fresh computations. A hidden page is not a security check.
4. Keep development bound to localhost. For team access, document the actual host, encrypted access method and private token distribution. Do not expose an unauthenticated service on `0.0.0.0` merely to make six browsers connect. Existing organization login may replace token login only with verified role mapping.

### 2. Move long paid execution out of UI reruns

Extend the service with `submit_answer(principal, request) -> request_id`, `request_status(principal, request_id) -> RequestView`, and `cancel_request(principal, request_id)`. Synchronous `answer` remains the shared worker implementation and controlled CLI entry.

1. Create a bounded process-owned `ThreadPoolExecutor` with at most six workers and bounded pending capacity. Use a semaphore/admission guard before submission; default total admitted unfinished requests is 12. Queue saturation returns a visible local rejection before paid work. No persistent task broker is needed.
2. Persist the input snapshot/idempotency key before submitting. Under a short transaction, claim `queued -> running` exactly once. Concurrent duplicate submissions return the same request. If local scheduling fails, mark technical failure; no unused reservation may remain.
3. Worker receives immutable request/principal IDs, obtains allowed current settings, uses the shared retrieval/gateway functions and persists result/attempt states. It never calls Streamlit functions or mutates `st.session_state`.
4. Keep the synchronous SDK's settings immutable. Verify the pinned client's concurrent transport behavior with six worker calls. If its documented runtime cannot share safely, use service-owned per-worker clients and close every one on controlled shutdown; do not mutate one global key/model per member.
5. Request status distinguishes `queued|running|completed|failed|cancelled|interrupted`. Domain status and billing state are separate fields. Queued cancellation is conclusive before dispatch; running cancellation suppresses further stages/rendering but cannot assert the active provider call cost zero.
6. On controlled stop, reject new submissions, wait a bounded time for workers, persist unfinished state and close owned clients. On restart, queued requests become interrupted unless safely requeued before any dispatch; running attempts recover as unknown. No automatic replay of uncertain paid work.

### 3. Give UI state explicit ownership

Session state contains authenticated session ID, selected scope/version, mode, as-of date, current question hash, generation ID and active request ID. Submission generates a fresh generation/idempotency ID once; reruns reuse it and never resubmit automatically.

Capture a request's state on submission. Changing scope, question, mode or date invalidates its permission to attach to the current answer panel. Polling may store its historical outcome, but displays it only if request/generation/scope hashes still match. Clear previous answer/evidence before a new request begins or target changes. A failed request cannot leave an old answer under a new title.

Navigating away/logout cancels screen ownership and requests cancellation of queued work. Backend workers finish billing independently. Reopening a permitted prior request can show its immutable scope snapshot explicitly; it does not transform it into the current query's answer.

### 4. Implement reactive, read-only status

Use a one- or two-second Streamlit fragment to poll budget and request status. Keep network inference in the worker, so the main script returns promptly and fragment scheduling can continue. The fragment performs reads only; it must never call `submit_answer`, generation, embedding, indexing or LLM judging. [Streamlit fragments](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment) support timed partial reruns, whose scheduling still requires real-browser verification.

Show reserved maximum when a request starts, settled measured cost when usage arrives, and unknown pending cost after interruption. The shared strip updates for other members as well. Warn at 50%, 75%, 90% of the $16 operational cap and when cumulative spend runs ahead of the configured project timeline. Keep the $20 percentage denominator distinct from cap warnings.

Set an explicit freshness label when the database/reconciliation is unavailable; do not render stale numbers as live credit. A failed ledger read cannot permit paid work. At cap exhaustion, disable paid actions with a clear reason but retain filters, keyword retrieval, original evidence and downloads.

### 5. Complete deterministic modes and balanced comparisons

Metadata questions return free typed/provenance-backed results. Unknown or conflicted facts keep their status. Inventory mode enumerates structured requirements within the selected source and declares extraction completeness; it does not ask top-k passages to promise every requirement.

For two-document comparison, create scoped retrieval subqueries for each selected document. At most two bounded deterministic subqueries, no paid rewriting. Start with up to three evidence units and roughly half the target evidence budget per document; redistribute unused room only after both have had a coverage attempt. Hard global evidence ceiling remains 5,000 tokens after expansion.

Each document contributes evidence or an explicit missing/unavailable entry. A global top-k from the larger RFP cannot consume the comparison context. If minimum complete facts/qualifiers cannot fit, explain the coverage limit or narrow the question; never silently answer only one side. Test wrong-document codes, shared hashes, different revisions and conflicting metadata.

### 6. Complete source navigation and safe exports

Construct citation targets from the persisted evidence map. Resolve original download through the current principal and `(doc_id, source_hash)` allowlist; reject arbitrary filenames/paths, traversal and source-version mismatch. Serve a bounded excerpt, surrounding section and original download; an optional PDF viewer must navigate physical pages accurately before it replaces the basic evidence panel.

Escape model/source markup and keep unsafe HTML disabled. Do not expose hidden table text as active UI code. Display source warning/review status alongside evidence. Saved trace exports carry stable locations and hashes, not only mutable chunk IDs.

Verifier corrections append reviewer/time/reason, quoted original evidence and affected row/run IDs. They do not silently overwrite sealed labels or make an old experiment appear to have used new labels. Config edits create a new version; exports include old snapshots.

### 7. Execute budget reconciliation and recovery scenarios

Implement owner-only reconciliation import with interval/project identity, dated provider evidence and unique reconciliation ID. Compare provider cost with the same local closed interval; resolve overlapping unknown attempts and record late-usage compensation as defined in the contract. Never add the entire provider total on top of existing settled costs.

Provide a recovery view showing unknown attempts, their reserved amounts, dispatch times, stage/member and evidence. Owner actions require a reason and create an audit event. “Release all older than one hour” is not an allowed operation. If provider scope/admin access is unavailable, show that limitation and use dated owner exports.

## Automated gate

Introduce `check --phase 3 --provider fake` and `load-check --users 6 --provider fake`. The former runs focused service/budget/state checks; the latter launches six concurrent service requests against one temporary ledger/index and a controllable delayed/failing transport.

| Scenario | Required outcome |
| --- | --- |
| Six users reserve near cap | Affordable dispatch count only, one ledger revision stream, no six independent balances |
| Same request submitted twice across reruns | One persisted request/paid attempt; changing payload under its key is a conflict |
| Scope/question changes during delayed answer | Late result cannot render under new scope; billed attempt still settles |
| Queued versus running cancellation | Queued request never dispatches; active unknown call retains reservation until evidence arrives |
| Login expiry/revocation between paid stages | No next dispatch; already received usage still settles |
| Consultant tries verifier/budget/sealed action directly | Service denial even if navigation is bypassed |
| Retry after billed failure | New explicit attempt/reservation; old cost remains counted |
| Crash after dispatch marker; duplicate completion after restart | Conservative recovery and exactly-once accounting |
| Provider interval adjustment imported twice, then late usage | No duplicated adjustment or late settlement double count |
| Ledger unavailable, price unknown, cost exceeds reservation | Paid mode fails closed or freezes; actual cost/error remains recorded |
| Exhausted cap and repeated polling | Free routes work; polling never dispatches a paid stage |

Fake delays should make races observable and deterministic. Test actual database transactions and persisted request state rather than a mocked counter. Mock only provider/model execution and controlled clock inputs where necessary.

## Browser verification

Run separate consultant and verifier sessions, including six active tabs/sessions. Record viewport, account role, active configuration and observed outcomes. The minimum walkthrough is:

1. Search a long Korean title; verify wrapping, filters and historical notice. Select HWP, ask, open table evidence and download the managed original.
2. Switch to PDF; open a citation on a late physical page and inspect original content. Try an ambiguous code without scope and a known quarantined source.
3. Compare two selected RFPs; ensure both contribute facts or limitations. Exercise missing amount/date and conflicting institution/deadline.
4. Delay a fake provider response; change scope/question, then confirm no stale answer attaches. Submit twice, navigate away, logout and inspect backend settlement.
5. In another session, initiate a call; confirm the first session's budget changes within the polling interval while a request is running. Distinguish reservation, settled cost and unknown cost.
6. Reach the temporary test cap; confirm generation blocks, free browsing works, refresh/polling creates no calls and unauthorized pages/actions remain denied.
7. Use keyboard navigation, focus/status labels, readable contrast and narrow layout. Save screenshots for layout/behavior evidence; no UI screenshot is claimed before this run.

Use the fake transport for failures/load. A bounded real consultant smoke through the production gateway is separate, explicitly estimated, and cannot recreate every race at the team's expense. Streaming is optional; if introduced, test missing final usage/refusal/truncation and never mark partial JSON complete.

## Exit and handoff

Both authorized workflows operate on the same source/index/gateway, six-user tests pass, budget refresh remains responsive during long calls, and source navigation/browser checks are recorded. README and runbook give the real launch/access/recovery procedure. Record unresolved hardware latency, hosting or authorization blockers honestly.

Write `.runtime/releases/phase-3/report.md` with role/browser matrix, fake concurrency/recovery outcomes, measured latency and polling behavior, screenshots, actual paid smoke cost, selected config and remaining envelopes. Pass this operational baseline and reviewed correction log to [phase 4](4-evaluation-and-release.md).
