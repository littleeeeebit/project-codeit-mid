`reserve` runs one transaction:
1. `require_owner`, `owner_guard`.
2. Refusals in order: paid_disabled, postgresql_maintenance, frozen, unknown_rate, unknown_purpose, above_consented_maximum, cap_exhausted (cap − spent − pending), envelope_exhausted (shares embedding 1, gold_eval 3, interactive 12, judge_eval 0, ocr 0).
3. Otherwise an `attempts` row is inserted in state 'reserved' with price_json (rates, rate_version, billing_scope of the member's key), and `_bump` updates the ledger revision.
Database lock or timeout errors fail closed as `ledger_unavailable`.