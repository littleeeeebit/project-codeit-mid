Thin wrappers, each calling one service function:
- `_account_routes`: info, update, budget, api-key, model.
- `_ask_routes`: documents, ask, requests, stream.
- `_request_routes`: cancel, abandon, export, evidence, originals.
- `_verify_routes`: traces, evaluation, judges, maintenance, experiments, fidelity, ingestion, corrections, history, gold second review.
- `_dataset_routes`: drafting and gold. Registered last, so `/api/gold/recent` is not shadowed by `/api/gold/{candidate_id}`.
Dates are fixed to `date.today()` at the route; there is no as-of control.