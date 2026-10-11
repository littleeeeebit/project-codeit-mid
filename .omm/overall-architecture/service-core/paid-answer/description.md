`_execute` opens one Langfuse trace (`answer-question`, seed = request_id). `_execute_traced` maps outcomes: `_Stop` → cancelled/interrupted, `ServiceError` → clarification_required/failed, any other exception → technical_error/failed. `_paid_answer` then:
1. Short-circuits `ingestion_unavailable` when every scoped document is unparsed or unindexed.
2. Resolves a frozen verifier run (its exact evidence, no new retrieval).
3. Runs `_checkpoint`, then `_rewrite` for follow-ups, then `_prepare_paid` (single / per-document compare / corpus).
4. Checks `_above_consented` for frozen runs; empty evidence gives `insufficient_evidence`.
5. Runs `_checkpoint`, then `_generate` streaming into `res.partials`.
6. `_validated` (generation.validate_answer against stored chunk bodies) produces the final payload.
Every exit goes through `_finish`, which derives billing_state from `attempts` and persists trace_json and result_json.