# Operations runbook (phase 3)

This runbook covers launching, giving access, stopping, recovering and reconciling the single shared application. Every command below exists in this repository. Steps that depend on a host or network decision the owner has not yet made are marked **owner decision**. Do not read them as configured.

Run the commands with the project environment's interpreter from any directory. Paths in environment variables must be absolute.

## 1. One owner, one data directory

- One process owns `RFP_DATA_DIR` at a time. The serving app and every paid CLI job take `gateway.lock` there. A second owner is refused with `GatewayLockError`. Stop the UI before paid maintenance (`build-dense`, `evaluate-retrieval --allow-paid-queries`).
- The ledger, sessions, requests, audit events and corrections all live in `rfp.sqlite3` on a local disk. A network share is outside the contract.
- Private member records live in `RFP_ACCOUNTS_FILE`, by default `<RFP_DATA_DIR>/private/accounts.json`. That file is ignored by Git with the rest of `.runtime/`. The operational config (`RFP_CONFIG_FILE`) must not contain secrets and rejects an `accounts_file` key.

## 2. Access modes

| Mode | When | What it allows |
| --- | --- | --- |
| Open (no accounts file) | The phase-1 owner decision, a single person on one machine | Consultant and verifier screens under a typed name that only attributes work. Budget administration and sealed data are unavailable. The UI refuses to run unless `--server.address` is `127.0.0.1`, `localhost` or `::1`. |
| Token (accounts file exists) | Any shared use | Login with member ID plus a personal random token. Server sessions last 8 h (`session_hours`). Roles are checked on every service call. |

Provision members on the owner host. Each token is printed **once**: deliver it privately (in person or through an encrypted messenger) and never in a shared channel or document.

```powershell
python -m rfp_assistant.cli accounts provision --member-id kim --capabilities consultant
python -m rfp_assistant.cli accounts provision --member-id lee --capabilities consultant,verifier
python -m rfp_assistant.cli accounts provision --member-id owner --capabilities consultant,verifier,budget_admin
python -m rfp_assistant.cli accounts list
```

- **Rotate** a token by running `provision` again for the same member. The old token and all of that member's sessions stop working.
- **Disable or enable** a member: `accounts disable --member-id kim` or `accounts enable --member-id kim`. Disabling ends that member's sessions.
- **End sessions without rotating:** `accounts revoke-sessions --member-id kim --reason "..."`. Omit `--member-id` to end everyone's sessions. The admin page has the same action with an audit event.
- **Failed logins:** five failures per account in 15 minutes lock that account for the rest of the window. Unknown names share a global limit. Every failure shows the same message.
- **`sealed_evaluator`:** grant it only for the phase-4 freeze procedure. Verifiers never see sealed test rows.

A browser reload starts a new Streamlit session, so the member logs in again. The session ID is deliberately not put in the URL. A paid request that is already running still settles on the server.

## 3. Launch

Local development and single-host use:

```powershell
python -m streamlit run app.py --server.address 127.0.0.1
```

**Team access is an owner decision.** No host, TLS endpoint or tunnel has been configured or verified by this phase. Use one of these patterns. Never bind an open-mode (no accounts) server to `0.0.0.0`.

- **Keep Streamlit on `127.0.0.1`** and give members an encrypted path to it. Examples: an SSH local forward (`ssh -L 8501:127.0.0.1:8501 <owner-host>`), or an existing organization VPN.
- **Put a TLS reverse proxy in front of `127.0.0.1:8501`** on the team host, with token mode enabled.

Record the chosen host, access method and who holds tokens in the phase-3 report (`report --phase 3`) once it exists.

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
- **Restart:** the next owner recovers conservatively and never replays anything:
  - queued and running requests become `interrupted`;
  - `dispatching` attempts become `unknown`, keeping their reserve as pending cost;
  - `reserved` attempts that never dispatched are released.

## 5. Budget recovery

The admin page (사용량 관리, `budget_admin` only) and the CLI show the same state:

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
