Paid and free admission:
1. `_validate_request` checks mode, question length, the scope count per mode and idempotency key bounds, and refuses unknown or outdated frozen verifier runs.
2. `_conversation` walks `previous_request_id` up to 6 turns; each turn must be the same member's, terminal, and the same scope.
3. Free modes (metadata, inventory) are created 'running' and executed inline.
4. Paid modes call `_create(..., 'queued', before_insert=admit)` in one transaction keyed by (member_id, idempotency_key). A duplicate key with the same input hash returns the prior request without consuming a slot; a different input is refused.
5. If `runner.submit` fails, the slot is released and the row marked failed (`_fail_unscheduled`).
`service.ask` uses a fresh UUID as both idempotency key and generation_id and returns `target_key`. The verifier's `generate_from_run` uses `vgen-<run_id>`, so repeated presses reuse one request.