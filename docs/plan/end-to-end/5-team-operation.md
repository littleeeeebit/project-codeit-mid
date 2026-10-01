# Phase 5 — shared operation for the remaining project weeks

Goal: let six members use and improve the delivered assistant for more than three weeks without losing source provenance, reproducibility or control of the shared $20 allowance. This is the post-release operating assignment; phases 1–4 remain the two-to-three-day delivery path.

Enter with the actual [phase-4 release](4-evaluation-and-release.md), runbook, active immutable index/config, budget baseline/reconciliation watermark and open failures. Read [shared contracts](implementation-contracts.md), [budget](../../rag/budget.md) and [evaluation](../../rag/evaluation.md). If the release is limited, preserve its declared scope while completing the listed missing checks.

## Ownership and durable deployment

Assign one operational owner and a backup owner from the team; record their roles in the runbook. There is no login: all six users connect to one application deployment whose network reach the owner decides. Keep the SQLite ledger and immutable artifacts on the owner's persistent local disk. Ephemeral hosting that can lose or reset the ledger is outside this baseline.

Hold a process-owner lock while the real paid service is active. Refuse a second UI/gateway owner against the same data directory. Maintenance CLI jobs acquire the same lock after the UI has stopped; fake checks use a separate temporary directory. Use the host's standard-library file locking and explicit handle cleanup rather than a new coordination service. Confirm lock recovery after process death; do not delete a live lock to bypass it.

Document the real interpreter, absolute application/data/source paths, who can reach the host and how, port and controlled shutdown. The developer's localhost command alone does not prove shared deployment. Verify the first owner/backup-owner restart before declaring the runbook complete.

## Operating cadence

| When | Action | Paid behavior |
| --- | --- | --- |
| Startup | Verify database/schema, owner lock, rate/config fingerprint, active index hashes and pending-attempt recovery | None until paid configuration and recovery checks pass |
| Every request | Record the visitor name, snapshot scope/config, reserve/dispatch/settle, record trace and display freshness | One bounded generation plus uncached embedding only when its route needs it |
| Daily owner check | Inspect settled/pending totals, pacing, unusual retries, failed source states and ledger backup | Read-only; no evaluation rerun |
| Provider data becomes available | Reconcile one closed matching interval; audit external usage and unknown attempts | Read-only billing import; no raw-key experiment |
| A source/config changes | Review change, build an immutable candidate, retrieval-first regression, estimate any needed embeddings | Only uncached approved payloads; no full-corpus re-embedding by default |
| Weekly review | Pick recurring failure categories, compare one justified change, inspect account/rate/dependency drift | Paid finalists/judges only within an explicit estimate |
| Project close | Final reconciliation, unresolved-attempt report, reproducible handoff and access/key return according to owner policy | No new batch/interactive paid work |

This is a manual operating cadence. Do not create scheduled automations, contact members or change account settings merely because the plan names a cadence.

## Ordered work

### 1. Verify launch, ownership and access

1. Run the release manifest/check commands on the actual shared host with paid mode initially disabled. Verify persistent data survives an application restart and machine/session restart as applicable.
2. Confirm second-owner refusal and offline maintenance lock behavior. No member runs a separate academy-key application with its own ledger. If outside use is unavoidable, record that real-time scope is incomplete and reconcile dated owner evidence.
3. There is no login: confirm the deployment's network exposure matches the owner's decision, that members type consistent names for attribution, and that the API key never appears in Git, screenshots or exports. Confirm sealed rows stay off the verifier page and every admin action leaves an audit event.
4. Record startup/shutdown procedure and recovery status. Re-enabling paid mode after recovery requires known-safe configuration and conservative handling of unresolved billing, not a clean-looking empty dashboard.

### 2. Back up and restore consistent state

Use `sqlite3.Connection.backup()` for a consistent database snapshot rather than copying a live main file while ignoring WAL. Store backups outside the active runtime directory under owner-controlled access. Include a manifest of referenced extraction/index/dataset/report hashes and the budget reconciliation watermark. Private secret configuration is backed up separately by the owner, never in shareable reports.

Define `backup --destination <absolute-directory>` and `restore-check --backup <absolute-manifest>` in the maintenance CLI. Backup refuses unsafe or overlapping active destinations. Restore checks operate only in a fresh staging directory and keep fake transport/paid-disabled regardless of copied settings.

Verify restored prior spending, pending/unknown attempts, settled costs, active index row mappings and source evidence. A restore cannot reset the allowance or resume uncertain paid calls. Before real recovery, stop the old owner; stage and inspect the backup, reconcile spending after its watermark, then activate it with an auditable reason. Never let old and restored owners dispatch concurrently.

Keep the last known-good release/index and recent consistent backups. Retention of old artifacts requires checking references from gold/traces/citations. Do not delete referenced source/extraction versions merely because a newer index exists.

### 3. Reconcile billing without double counting

Select a known closed provider interval and the correct academy project/allowance scope. If provider costs include unrelated teams and cannot be filtered, record unavailable reconciliation rather than guessing this team's share.

Review overlapping unknown attempts before applying the difference from local settled totals. Each adjustment has unique ID, scope, interval, evidence, actor and reason. Repeated imports are idempotent; later usage for an interval already covered by reconciliation is compensated so total spend does not increase twice for the same call.

Keep a visible watermark and unresolved cost amount. Month boundaries do not reset the $20 denominator or local cap. Changed prices create new snapshots for future calls; past attempts retain their original snapshots. A changed model/service tier with no verified rate disables that paid route.

When settled cost unexpectedly exceeds a reservation or direct-key usage consumes the safety margin, freeze new paid work, retain free browsing, reconcile and revise approved envelopes. Owner adjustments cannot invent credit or turn uncertainty into an automatic refund.

### 4. Fix failures in the right stage

Record new consultant failures with question/scope/as-of, pinned source/index/prompt/model versions, trace, reviewer evidence, error category and severity. The order of investigation is:

1. Original extraction/table fidelity and metadata provenance.
2. Wrong document/version, exact identifiers and ambiguous scope.
3. Missing candidate evidence and analyzer/query representation.
4. Parent expansion, deduplication, qualifier loss and token packing.
5. Unsupported claims, prompt boundaries and output validation.
6. Model/reranker changes only after the preceding cause is ruled out.

For each accepted correction, add a independently reviewed regression row with original source spans. Keep it separate from the sealed release test. Run keyword/retrieval checks first; use paid generation only for the affected behavior or a bounded justified sample. Do not optimize against the original sealed answers and continue calling their score independent.

### 5. Change an index/config safely

1. Freeze changed source bytes and review relevant original evidence. Preserve previous association/version mappings and record provenance corrections.
2. Plan changed unique payloads and cached vectors. Owner approves the concrete estimate through the existing action; the global/category caps remain authoritative.
3. In maintenance mode, build the candidate in a new immutable directory through the gateway. Validate artifacts, dimensions/order and source-span mappings before readiness.
4. Run focused retrieval regression and critical identity/qualifier checks. Compare against the last active version with the same labels and budget. Optional dense/reranker gains must still meet their original gates.
5. Activate the candidate transactionally with a decision record. Existing in-flight/saved requests retain their snapshot; future requests use the new version. Cache keys change with index/config/prompt/auth scope as appropriate.
6. If regression is found, reactivate the retained known-good version. Rollback does not roll back charged usage or erase traces. Record the failing version and cause.

Keep changes small and measured. Add FAISS, a database server, Langfuse or an existing LiteLLM deployment only when current volume/runtime/operational failures justify it. Migration must retain allowance history, source identity, idempotency and role boundaries; a new dashboard cannot initialize spending to zero.

### 6. Close the project with reusable evidence

Record final settled/pending/provider-reconciled totals, model/config/rate snapshots, reviewed corpus coverage, unresolved source/quality cases and the last known-good release. Preserve sealed test provenance and identify post-test regressions separately.

The backup owner follows the runbook on a staged copy and demonstrates source-backed retrieval plus cost-state inspection without paid dispatch. Export sanitized reports and documentation; return/revoke academy access only under the actual owner's instructions. Do not publish raw keys, source files or a new deployment merely to make the handoff convenient.

## Runnable operating checks

Implement these as existing-CLI extensions, using temporary/fake state for fault checks:

| Command | Exit evidence |
| --- | --- |
| `check --phase 5 --provider fake` | Owner-lock lifecycle, backup/restore, allowance persistence, incremental activation/rollback and reconciliation invariants pass |
| `budget-report` | Read-only total/pending/available/category/member report with watermark; no key/provider inference call |
| `backup --destination <absolute-directory>` | Consistent snapshot and referenced-artifact manifest written; original state unchanged |
| `restore-check --backup <absolute-manifest>` | Fresh staging copy verifies source/index/ledger hashes; paid mode remains disabled |
| `reconcile --file <absolute-path>` | Owner-only unique closed-interval import; duplicate is harmless; unknown/late-usage behavior audited |
| `activate-run --run-id <id> --decision-file <absolute-path>` | Only a ready verified config becomes active; same action supports explicit known-good rollback |

Use a restore fixture with a settled charge, prior-use adjustment and unresolved attempt. After restoring, available funds must match conservative accounting exactly. Use a reconciliation fixture where an unknown 60-microdollar attempt is covered by a 60-microdollar provider interval, then late usage arrives: total cost stays 60, not 120, and no pending double count remains.

## Exit and continuing limits

This phase is operationally ready when both owner and backup owner can launch, stop, inspect usage, recover unknown attempts, restore a staged backup and activate/roll back an index from the runbook. Paid routes remain within verified tracking scope and conservative admission; free discovery survives budget exhaustion. Ongoing real usage and weekly improvements produce actual logs rather than a claim that three weeks have already elapsed.

Write `docs/operations/operating-handoff.md` with real host/setup details safe to share, ownership, backup/recovery results, cadence, final known limitations and links to actual release evidence. A successor can operate the system without undocumented machine state. This completes the plan's handoff path; it does not authorize scheduled jobs, external messages or account changes during this documentation task.
