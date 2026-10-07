The request and billing records:
- `requests`: unique on member_id + idempotency_key, with status, cancel_requested, `request_json` (the input snapshot), `trace_json`, and `result_json` holding the evidence map.
- `attempts`: one row per paid stage, unique per stage, model and response id.
- `adjustments`: append-only and idempotent by correction_key; includes reconciliation intervals.
- `budget_settings`: a single row with cap, envelopes, rates, paid_enabled, frozen_reason and revision.
- `audit_events` and `database_control`.