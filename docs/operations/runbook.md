# Operations runbook (phases 3–4)

This runbook covers launching, giving access, stopping, recovering and reconciling the single shared application, and (sections 9–12) the phase-4 setup, evaluation, sealed run, release, index rollback and backup procedures. Every command below exists in this repository. Steps that depend on a host or network decision the owner has not yet made are marked **owner decision**. Do not read them as configured.

Run the commands with the project environment's interpreter from any directory. Paths in environment variables must be absolute.

## 1. One owner, one data directory

- One process owns `RFP_DATA_DIR` at a time. The serving app and every paid CLI job take `gateway.lock` there. A second owner is refused with `GatewayLockError`. Stop the UI before paid maintenance (`build-dense`, `evaluate-retrieval --allow-paid-queries`).
- The ledger, requests, audit events and corrections all live in `rfp.sqlite3` on a local disk. A network share is outside the contract.
- The operational config (`RFP_CONFIG_FILE`) holds no secrets. `OPENAI_API_KEY` stays in the server environment, `.env` or Streamlit secrets.

## 2. Access: no login

There is no login (owner decision 2026-09-30, reaffirmed for phase 3 on 2026-10-01). Every visitor gets every page: 컨설턴트, 검증, 질문 검토 and 사용량 관리. The sidebar name (default `owner`) is recorded on requests, attempts, reviews, corrections and audit events.

- Attribution, not authentication. Per-member spend in the sidebar is only as accurate as the names people type. Ask each member to use one consistent name.
- Network reach is the access control. Anyone who can open the page can spend the allowance and use the admin page. Admin actions still require a reason and leave an audit event with the typed name; there is still no bulk release of unknown billing.
- Sealed test rows are never served to the verifier page. Only the phase-4 freeze procedure, run by the owner from the CLI, reads `sealed/`.
- A browser reload resets the sidebar name to `owner`; retype it. Running requests still settle on the server, and the reloaded page shows them under "내 최근 요청" for that name.

## 3. Launch

Local development and single-host use:

```powershell
python -m streamlit run app.py --server.address 127.0.0.1
```

Team access is an **owner decision**. No host, tunnel or network has been configured or verified by this phase. Because there is no login, binding to `0.0.0.0` exposes spending and administration to everyone on that network. Choose deliberately:

- Keep Streamlit on `127.0.0.1` and give members an encrypted path to it. Examples: an SSH local forward (`ssh -L 8501:127.0.0.1:8501 <owner-host>`), or an existing organization VPN.
- Bind to a trusted team network only (`--server.address <team-network address>`) when every person on that network may spend the allowance.

Record the chosen host and who can reach it in the phase-3 report (`report --phase 3`) once it exists.

Paid generation stays disabled until `configure-budget` records the dates, prior use, allowance and cap (see the README). Fake-provider demonstrations use a config file with `{"provider": "fake"}` and optionally `"fake_delay_seconds": 4`. Such a config never builds a real SDK client.

## 4. Request execution and controlled stop

- Paid answers run on a process-owned executor with 6 workers and at most 12 admitted unfinished requests (`request_workers`, `request_admission`). A full queue is refused before any paid work.
- Re-submitting the same request (a rerun, a double click) returns the same request. The same key with a different question is refused.
- Controlled stop: stop Streamlit with Ctrl+C (SIGINT/SIGTERM). The resource owner then:
  1. stops accepting work;
  2. waits up to `shutdown_wait_seconds` (20 s) for running workers;
  3. marks queued and unfinished requests `interrupted`;
  4. closes the SDK client and releases the lock.
- A worker still inside a provider call keeps its attempt `dispatching`. That attempt becomes `unknown` at the next start.
- A worker that has not yet dispatched never starts a new paid stage once stop begins. That covers query embedding and generation, even after its retrieval finishes. The stop check and the `dispatching` marker share one ledger transaction. Such a request ends `interrupted`, and its reservation, if any, is released.
- The stop starts from the interpreter's exit as soon as Streamlit's signal handler ends the server. It runs before the executor waits for its workers, so a running request stops at its next paid stage instead of finishing first.
- Verifier generation: the 검증 page's paid button generates from the frozen run's own evidence and 기준일 (`verifier_run_id`). It never retrieves again, so a query-vector cache miss cannot switch it to hybrid and no query embedding is paid. The reservation never exceeds the displayed maximum. The maximum is passed into the atomic admission, so even a rate change between estimate and reservation refuses the request (`above_consented_maximum`) without a call. If the index, serving run, prompt or model changed since the run was frozen, the button is withheld and the service refuses the run. Make a new run instead. A two-document run needs an evidence-unit limit of at least 2, one per document, and the form enforces that minimum.
- Restart: the next owner recovers conservatively and never replays anything:
  - queued and running requests become `interrupted`;
  - `dispatching` attempts become `unknown`, keeping their reserve as pending cost;
  - `reserved` attempts that never dispatched are released.

## 5. Budget recovery

The admin page (사용량 관리) and the CLI show the same state:

```powershell
python -m rfp_assistant.cli budget-status
python -m rfp_assistant.cli unresolved
```

An `unknown` attempt holds its full reservation until evidence arrives. There is no bulk release, and age alone never releases anything. Resolve one attempt with exactly one of these, each with a reason and an audit event:

1. Settle from usage evidence. On the admin page, use the 미확정 비용 → 정산 form with the provider's dated usage for that call: input, output and cached tokens, plus an evidence location. Settlement happens exactly once; a duplicate completion changes nothing.
2. Reconcile a closed provider interval. Build a JSON record and import it:

   ```json
   {"reconciliation_id": "2026-10-01-daily", "interval_start": "2026-10-01T00:00:00+00:00",
    "interval_end": "2026-10-01T23:59:59+00:00", "provider_total_micro_usd": 412345,
    "scope": "<provider project>", "evidence": "<dated export file or dashboard capture>",
    "covered_attempt_ids": ["<unknown attempt dispatched inside the interval>"]}
   ```

   ```powershell
   python -m rfp_assistant.cli reconcile --file C:\abs\reconcile-2026-10-01.json
   ```

   The adjustment is the provider total minus local settled cost for the same interval. Covered attempts must be `unknown` and dispatched inside the interval; they move to `reconciled`. Re-importing the same record is harmless. The same ID with a different total is refused, so a changed provider total needs an owner correction. Late usage for a covered attempt settles it and adds one compensating negative adjustment (`late-settlement:<attempt>`).
3. Spend outside the gateway (a notebook, a direct key): record it on the admin page under 외부 사용 조정, with a unique key, the amount, evidence and a reason. A key cannot be applied twice.

If the provider's billing scope or owner export is unavailable, keep the attempts `unknown`. The sidebar then shows the unknown amount and the last reconciliation time, so nobody mistakes local estimates for provider credit.

Freeze. If a settled cost ever exceeds its reservation, new paid work freezes. Inspect token counting and rates first. Then resume on the admin page (유료 호출 상태, with a reason) or with `configure-budget`.

## 6. Cap and pacing

- Sidebar warnings: the highest of 50 %, 75 % and 90 % of the operational cap (spent plus pending), "ahead of pace" when committed spend exceeds the linear share of the project dates, and unknown billing. The $20 percentage is shown separately from these cap warnings.
- At the cap: paid answers are refused before dispatch. Search, filters, basic information, requirement lists, evidence and original downloads keep working.
- Polling: the budget strip polls every 2 s and the request panel every 1 s. Both are read-only; refreshes and polling never dispatch a call.

## 7. Checks

```powershell
python -m rfp_assistant.cli check --phase 3 --provider fake          # service, budget and request-state gate
python -m rfp_assistant.cli load-check --users 6 --provider fake --save   # six concurrent members, temporary ledger
python -m rfp_assistant.cli load-check --users 6 --provider fake --fail-every 3   # injected post-dispatch timeouts
python -m rfp_assistant.cli report --phase 3                          # .runtime/releases/phase-3/report.md
python -m rfp_assistant.cli check --phase 4 --provider fake          # gold, evaluation, sealed run, backup, report
python -m rfp_assistant.cli check --phase all --provider fake --save # every test; --save records it for release-report
```

`load-check` builds its own temporary corpus and ledger and never touches `RFP_DATA_DIR`. Only `--save` writes its JSON result next to the phase-3 report. A real paid smoke test is a separate owner-approved action with an explicit estimate. It cannot reproduce the races these fake checks exercise.

## 8. Managed verification

`verification.json` is the manifest for the local verification service (`wiki-agent/local-verification`). It lists the committed contracts, the prose documents, and every major flow. Each flow has a kind (`command` or `browser`), one command, the impacted paths, the environments it uses, and the assertions it must observe. Each flow command is `python -B tools/verify.py <flow-id>`. Its last output is exactly one `local-evidence` block: the head, flow, `environment_id`, `test_scope`, and one observation per assertion (`id`, the exact `expected`, the observed `actual`, `pass`). Browser flows also record the requests sent to the served origin, the browser tool, the build head read from the served app's sidebar (`빌드 <sha>`), and the browser actions.

- Owner settings live in `.wiki/verification.local.json`, saved through the local review settings and bound to the manifest digest. The file is git-ignored, and the implementer never supplies it.
- Corpus and origin: the service copies the settings' `env_file` to the checkout's `.env` without exporting it. The runner reads these keys from that `.env`, and from the process environment only for keys `.env` does not set: `RFP_SOURCE_DIR`, `RFP_DATA_DIR`, `RFP_VERIFY_ORIGIN`, `RFP_VERIFY_BROWSER_EXECUTABLE`, `RFP_VERIFY_QUESTION`, `RFP_VERIFY_HWP_DOC_ID` and `RFP_VERIFY_PDF_DOC_ID`. It never reads `OPENAI_API_KEY`. `RFP_VERIFY_ENV_FILE` points the runner at another file; tests use it to stay on the fixture corpus.
  - Dataset flows read the configured corpus through an isolated copy. The database is backed up into a temporary runtime and the artifact folders are linked read-only. Requests, verifier runs and fake ledger rows never reach the configured runtime.
  - An incomplete or unavailable corpus is a failed observation that names what is missing. Under the service, a dataset flow without any corpus also fails. Only a manual run outside the service falls back to the fixture corpus, and its observation says so.
- Provider: every flow uses the fake provider. `OPENAI_API_KEY` is removed from child processes.
- Browser flows serve the app on `RFP_VERIFY_ORIGIN` (default `http://127.0.0.1:8765`; include it in `allowed_origins`). They drive the app with Playwright (`pip install -e .[verify]`). `RFP_VERIFY_BROWSER_EXECUTABLE` selects a browser binary; otherwise Playwright's Chromium and then the installed Chrome are tried.
- On Windows, `controlled-stop` sends a real `CTRL_C_EVENT` to a hidden console child.
- The 15 flows cover access, the four answer modes, the request lifecycle, controlled stop, the shared budget, recovery, verifier runs, the repository gates (including `check --phase 4`) and the phase-4 evaluation and release path (`evaluation-release`: its unit tests plus a 31-step CLI walkthrough on a temporary fixture corpus, `tools/verification/phase4_walkthrough.py`) as commands. Six browser flows cover the consultant answer and evidence, the history, verifier generation, six concurrent named sessions, cap exhaustion with audited owner actions, and keyboard, focus, contrast and narrow layout. `python -B tools/verify.py --list` shows them.
- The evidence block is printed as the last three lines (one compact JSON line), so it survives the service's 80-line tail.

## 9. Setup and the API key

1. Create the environment and install the pinned dependencies (README, "Environment"). The tested host is Windows with Python 3.12; this repository's cloud checks ran on Linux with Python 3.12.
2. Place `원본 데이터/data_list.csv` and `원본 데이터/files/` under the repository, or point `RFP_SOURCE_DIR` at them (absolute path). `RFP_DATA_DIR` (absolute) moves the runtime; the default is `.runtime/` in the repository.
3. API key placement. Put `OPENAI_API_KEY` in the server's process environment, or in the repository's `.env` (git-ignored), or in `.streamlit/secrets.toml` (git-ignored). Never put it in `RFP_CONFIG_FILE`, a report, an export, a screenshot or a commit. Members never receive the key; they use the shared application.
4. `python -m rfp_assistant.cli init --paid-disabled`, then `manifest`, `ingest`, `build-keyword --include-unreviewed` (README).
5. Paid generation stays off until `configure-budget` records the dates, the prior use with its evidence, the allowance, the cap and the confirmed rates.
6. Run `check --phase all --provider fake --save` on the host before the first paid action.

## 10. Phase 4: evaluation, sealed run and release

Plan: [4-evaluation-and-release.md](../plan/end-to-end/4-evaluation-and-release.md). Paid steps are marked **paid**; each runs only with an explicit estimate ID, uses the same gateway and ledger as the application (envelope `gold_eval`), and needs the UI stopped (`run-answers` and `latency-run` take the gateway lock). Nothing below runs a paid LLM judge; disputed items go to blind human review.

### 10.1 Reviewed gold

```powershell
python -m rfp_assistant.cli gold submit --file C:\abs\dev-batch.jsonl --batch dev-b1 --dataset dev --drafted-by <drafter>
python -m rfp_assistant.cli gold submit --file C:\abs\test-batch.jsonl --batch test-b1 --dataset test --drafted-by <drafter>
python -m rfp_assistant.cli validate-gold --dataset dev
python -m rfp_assistant.cli validate-gold --dataset test        # prints IDs and counts only, never question text
python -m rfp_assistant.cli freeze-dataset --dataset dev --actor <owner> --reason "<why this set>"
python -m rfp_assistant.cli freeze-dataset --dataset test --actor <owner> --reason "<why this set>"
```

- Row shape, evidence groups, typed claims and negatives: `.wiki/gold-drafting.md`, section "Phase 4 gold rows".
- Development rows are reviewed on `질문 검토`. The reviewer differs from the drafter, ticks "원문 파일에서 … 직접 확인했습니다", and marks disputed deadlines, amounts, institutions and mandatory conditions; a third person records the second review under "2차 검토 대기".
- Sealed rows are reviewed only in the owner's terminal: `gold show --candidate-id <id>`, `gold decide ... --original-inspected`, `gold second-review ...`. They live under `.runtime/sealed/`; the 검증 and 질문 검토 pages never show them, and `evaluate-retrieval`/`plan-run --action answer-finalists` refuse the `test` split.
- A set below the per-type targets (60 + 60) validates as `pilot`. Report it as a pilot; never call it gold.

### 10.2 Retrieval-first development comparison (free)

```powershell
python -m rfp_assistant.cli evaluate-retrieval --dataset dev --runs K0,K1      # D,H reuse cached query vectors
python -m rfp_assistant.cli compare-runs --run-id <K1 run> --run-id <other run>
python -m rfp_assistant.cli draft-activation --runs <run>,<run> --out C:\abs\decision.json
python -m rfp_assistant.cli activate-run --run-id <run> --decision-file C:\abs\decision.json
```

Each run records the Git revision (or says none was available), whether tracked files differed from it, the package-source and metric-code hashes, and the hardware. Two-document rows are scored per document (recall and complete coverage only); gold-2 nDCG@5 uses `(2^grade−1)/log2(rank+1)` counted in required evidence groups, the unit of its ideal (one complete unit per group), with family-grouped bootstrap intervals (seed 20261001, 1000 resamples).

Returned top-5 passages outside every gold label are counted (`ndcg_pool`) and listed per row (`unlabelled_chunks@5` in the run's `traces.jsonl`). While any remain unreviewed, the recommendation is `pending_pool_review`: K1 stays provisionally and no finalist is named. Open each listed passage in its original. If it holds a required fact, add it as a new alternative (a new gold revision), validate, freeze and rerun. Otherwise record the review in the decision file before promoting a run or naming a finalist: `"pool_review": {"reviewed_by": "<name>", "runs": {"<run id>": <reviewed count>}}`, listing the promoted run, the finalist and the K1 baseline. K1 alone needs no review.

### 10.3 Answer finalists (paid)

```powershell
python -m rfp_assistant.cli plan-run --action answer-finalists --dataset dev [--runs <selected>,<finalist>]
python -m rfp_assistant.cli run-answers --estimate-id <id> --actor <owner>            # paid, UI stopped
python -m rfp_assistant.cli export-review --run-id <A-run>                            # blind sheet for reviewers
python -m rfp_assistant.cli import-review --run-id <A-run> --file C:\abs\reviewed.jsonl --reviewer <name>
```

- `plan-run` prices every remaining answer exactly as the answer path will (no provider call) and stores an estimate bound to the finalists, dataset population, prompt, model, output cap, rates and every row's token count. It refuses more than two finalists or finalists with different evidence ceilings. The 검증 › 평가 tab offers the same plan and a consented run on the application's own gateway.
- `run-answers` refuses a changed configuration or price ("plan again") and a maximum that no longer fits the envelope or cap. It stops at the first budget refusal or the first new unknown billing.
- Resume by planning and running again: finished rows are kept and never re-sent; a row whose call has unknown billing is skipped until it is settled or reconciled (§5), then runs once more as a recorded new attempt.
- Deterministic scoring covers typed numbers and dates (with qualifiers such as VAT), negatives and link validity. Text claims, partially supporting citations and unlabelled citations are "needs review" or "unjudged" until the blind sheet is imported. Citation precision is reported twice: over judged links and as a lower bound that counts unjudged links as unsupported.

### 10.4 Release freeze and the sealed run (paid, once)

```powershell
python -m rfp_assistant.cli freeze-release --run-id <activated run> --answer-run <A-run> --decided-by <owner> --rationale "<benefit, critical regressions, latency, cost>"
python -m rfp_assistant.cli plan-run --action sealed --freeze-id <F-id>
python -m rfp_assistant.cli run-answers --estimate-id <id> --actor <owner>
```

- `freeze-release` refuses unless the selected run is the activated one and servable, both splits are validated, frozen and unchanged, and a complete development answer run used that run. It records code, metric-code, prompt, model, rates, settings and dataset/review-log hashes in `.runtime/releases/<F-id>/freeze.json` with an audit event.
- The sealed run executes once. Any later change of code, prompt, model, rates, activation or datasets breaks the freeze. An interrupted sealed run resumes under the same freeze; a completed one stands.
- A later run on the same test set must be planned with `--post-test-regression` and run with `--reason`; it is labeled post-test regression. A new reliability claim needs a freshly drafted and independently sealed test set.

### 10.5 Latency sample (paid, bounded)

```powershell
python -m rfp_assistant.cli plan-run --action latency --waves 5 --users 6
python -m rfp_assistant.cli latency-run --estimate-id <id> --actor <owner>
```

Five waves of six concurrent answers (30 samples) on the activated configuration. The result records n, failures, per-wave timing, the first (cold) wave and the warm waves separately under `.runtime/releases/latency/`. It is a preliminary sample, not an SLA; a run with the fake provider is labeled `fake` and is not model latency.

### 10.6 Release report and mentor walkthrough

```powershell
python -m rfp_assistant.cli check --phase all --provider fake --save
python -m rfp_assistant.cli release-report --latest
```

The report (`.runtime/releases/<F-id or draft-date>/report.md` plus `manifest.json`, `coverage.json`, `evaluation.json`, `budget.json`) reads recorded evidence only and never generates. It marks the release `ready`, `limited` or `blocked`: blocked when a hard check failed (critical wrong value, scope leakage, an unresolvable link, the cap or a frozen ledger, a failed check run); limited when a hard check is unverified, a quality target is missed or unmeasured, or the evidence is pilot-only. Copy a sanitized version to `docs/operations/release-report.md`.

Mentor walkthrough on the frozen candidate (record the outcome as `.runtime/releases/<F-id>/walkthrough-results.json` in the shape of the phase-3 browser results):

1. 컨설턴트: search a project, select it, ask about a fact beyond the opening pages; show a claim, open its evidence (exact quote, location), download the original.
2. Show the shared cost strip before and after (settled cost of that request).
3. Select the duplicated/conflicting record pair and show the conflict state; show a document with missing metadata (기본 정보) and the quarantined/unsupported source.
4. Select two documents and run a balanced comparison.
5. Change the scope while a request runs: the old answer stays history.
6. 검증: the trace of the same question, a comparison of two frozen runs, and the 평가 tab (development scores, the sealed set as a count only, the release decision).
7. A budget cap and failure-state example: use a temporary fake-provider runtime (`load-check`, or a config with `{"provider": "fake"}` and its own `RFP_DATA_DIR`). Never drain the shared academy balance to demonstrate the cap.

## 11. Index activation and rollback

- `activate-run --run-id <id> --decision-file <abs>` switches serving in one transaction and appends the previous configuration to `activations`. Every earlier index directory stays on disk, so issued citations keep resolving.
- Rollback: activate the last known-good run again with a new decision file whose rationale names the failing version and why. Charged usage and traces are not rolled back. `report --phase 2` lists earlier activations.
- An activation changes what the sealed freeze recorded; do it before `freeze-release`, never between the freeze and the sealed run.

## 12. Backup and restore

```powershell
python -m rfp_assistant.cli backup --destination D:\rfp-backups\2026-10-02 --actor <owner>
python -m rfp_assistant.cli restore-check --backup D:\rfp-backups\2026-10-02\manifest.json
```

- `backup` uses SQLite's online backup API (consistent with WAL), copies `datasets/`, `sealed/`, `runs/` and `releases/`, and records the hashes of the immutable extraction and index artifacts it references, the ledger amounts and the reconciliation watermark. It refuses a relative, non-empty or overlapping destination. Keys and `.env` are not included; the owner backs them up separately.
- `restore-check` restores into a fresh staging directory with paid generation off and the fake provider. It verifies the database hash, schema, settled/pending/unknown/available amounts, prior use, adjustments, attempt states, copied files, extraction artifacts and the active index (and dense matrix) row mapping. It also previews restart recovery: unknown reserves stay pending; nothing is replayed. A summary goes to `.runtime/releases/restore-checks/`.
- Real recovery (phase 5): stop the old owner, restore-check the backup, reconcile spending after its watermark, then copy the database into place and start one owner. Never let the old and the restored owner dispatch concurrently, and never reset the allowance.
