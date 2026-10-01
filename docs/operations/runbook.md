# Operations runbook (phase 3)

This runbook covers launching, giving access, stopping, recovering and reconciling the single shared application. Every command below exists in this repository. Steps that depend on a host or network decision the owner has not yet made are marked **owner decision**. Do not read them as configured.

Run the commands with the project environment's interpreter from any directory. Paths in environment variables must be absolute.

## 1. One owner, one data directory

- One process owns `RFP_DATA_DIR` at a time. The serving app and every paid CLI job take `gateway.lock` there. A second owner is refused with `GatewayLockError`. Stop the UI before paid maintenance (`build-dense`, `evaluate-retrieval --allow-paid-queries`).
- The ledger, requests, audit events and corrections all live in `rfp.sqlite3` on a local disk. A network share is outside the contract.
- The operational config (`RFP_CONFIG_FILE`) holds no secrets. `OPENAI_API_KEY` stays in the server environment, `.env` or Streamlit secrets.

## 2. Access: no login

There is no login (owner decision 2026-09-30, reaffirmed for phase 3 on 2026-10-01). Every visitor gets every page: 컨설턴트, 검증, 질문 검토 and 사용량 관리. The sidebar name (default `owner`) is recorded on requests, attempts, reviews, corrections and audit events.

- **Attribution, not authentication.** Per-member spend in the sidebar is only as accurate as the names people type. Ask each member to use one consistent name.
- **Network reach is the access control.** Anyone who can open the page can spend the allowance and use the admin page. Admin actions still require a reason and leave an audit event with the typed name; there is still no bulk release of unknown billing.
- **Sealed test rows** are never served to the verifier page. Only the phase-4 freeze procedure, run by the owner from the CLI, reads `sealed/`.
- A browser reload resets the sidebar name to `owner`; retype it. Running requests still settle on the server, and the reloaded page shows them under "내 최근 요청" for that name.

## 3. Launch

Local development and single-host use:

```powershell
python -m streamlit run app.py --server.address 127.0.0.1
```

**Team access is an owner decision.** No host, tunnel or network has been configured or verified by this phase. Because there is no login, binding to `0.0.0.0` exposes spending and administration to everyone on that network. Choose deliberately:

- **Keep Streamlit on `127.0.0.1`** and give members an encrypted path to it. Examples: an SSH local forward (`ssh -L 8501:127.0.0.1:8501 <owner-host>`), or an existing organization VPN.
- **Bind to a trusted team network only** (`--server.address <team-network address>`) when every person on that network may spend the allowance.

Record the chosen host and who can reach it in the phase-3 report (`report --phase 3`) once it exists.

Paid generation stays disabled until `configure-budget` records the dates, prior use, allowance and cap (see the README). Fake-provider demonstrations use a config file with `{"provider": "fake"}` and optionally `"fake_delay_seconds": 4`. Such a config never builds a real SDK client.

## 4. Request execution and controlled stop

- Paid answers run on a process-owned executor with 6 workers and at most 12 admitted unfinished requests (`request_workers`, `request_admission`). A full queue is refused before any paid work.
- Re-submitting the same request (a rerun, a double click) returns the same request. The same key with a different question is refused.
- **Controlled stop:** stop Streamlit with Ctrl+C (SIGINT/SIGTERM). The resource owner then:
  1. stops accepting work;
  2. waits up to `shutdown_wait_seconds` (20 s) for running workers;
  3. marks queued and unfinished requests `interrupted`;
  4. closes the SDK client and releases the lock.
- A worker still inside a provider call keeps its attempt `dispatching`. That attempt becomes `unknown` at the next start.
- A worker that has not yet dispatched never starts a new paid stage once stop begins. That covers query embedding and generation, even after its retrieval finishes. The stop check and the `dispatching` marker share one ledger transaction. Such a request ends `interrupted`, and its reservation, if any, is released.
- The stop starts from the interpreter's exit as soon as Streamlit's signal handler ends the server. It runs before the executor waits for its workers, so a running request stops at its next paid stage instead of finishing first.
- **Verifier generation:** the 검증 page's paid button generates from the frozen run's own evidence and 기준일 (`verifier_run_id`). It never retrieves again, so a query-vector cache miss cannot switch it to hybrid and no query embedding is paid. The reservation never exceeds the displayed maximum. The maximum is passed into the atomic admission, so even a rate change between estimate and reservation refuses the request (`above_consented_maximum`) without a call. If the index, serving run, prompt or model changed since the run was frozen, the button is withheld and the service refuses the run. Make a new run instead. A two-document run needs an evidence-unit limit of at least 2, one per document, and the form enforces that minimum.
- **Restart:** the next owner recovers conservatively and never replays anything:
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

1. **Settle from usage evidence.** On the admin page, use the 미확정 비용 → 정산 form with the provider's dated usage for that call: input, output and cached tokens, plus an evidence location. Settlement happens exactly once; a duplicate completion changes nothing.
2. **Reconcile a closed provider interval.** Build a JSON record and import it:

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
3. **Spend outside the gateway** (a notebook, a direct key): record it on the admin page under 외부 사용 조정, with a unique key, the amount, evidence and a reason. A key cannot be applied twice.

If the provider's billing scope or owner export is unavailable, keep the attempts `unknown`. The sidebar then shows the unknown amount and the last reconciliation time, so nobody mistakes local estimates for provider credit.

**Freeze.** If a settled cost ever exceeds its reservation, new paid work freezes. Inspect token counting and rates first. Then resume on the admin page (유료 호출 상태, with a reason) or with `configure-budget`.

## 6. Cap and pacing

- **Sidebar warnings:** the highest of 50 %, 75 % and 90 % of the operational cap (spent plus pending), "ahead of pace" when committed spend exceeds the linear share of the project dates, and unknown billing. The $20 percentage is shown separately from these cap warnings.
- **At the cap:** paid answers are refused before dispatch. Search, filters, basic information, requirement lists, evidence and original downloads keep working.
- **Polling:** the budget strip polls every 2 s and the request panel every 1 s. Both are read-only; refreshes and polling never dispatch a call.

## 7. Checks

```powershell
python -m rfp_assistant.cli check --phase 3 --provider fake          # service, budget and request-state gate
python -m rfp_assistant.cli load-check --users 6 --provider fake --save   # six concurrent members, temporary ledger
python -m rfp_assistant.cli load-check --users 6 --provider fake --fail-every 3   # injected post-dispatch timeouts
python -m rfp_assistant.cli report --phase 3                          # .runtime/releases/phase-3/report.md
```

`load-check` builds its own temporary corpus and ledger and never touches `RFP_DATA_DIR`. Only `--save` writes its JSON result next to the phase-3 report. A real paid smoke test is a separate owner-approved action with an explicit estimate. It cannot reproduce the races these fake checks exercise.
