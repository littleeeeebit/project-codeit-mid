`_execute` opens one Langfuse trace seeded by request_id, and `_execute_traced` dispatches to `_metadata_answer`, `_inventory_answer` or `_paid_answer`. `_paid_answer` runs these steps:
- When every scoped document is unparsed or unindexed, it ends as `ingestion_unavailable`.
- `_checkpoint`.
- `_rewrite` for a follow-up.
- `_prepare_paid`: a frozen verifier run, a balanced compare through `_prepare_compare`, or `prepare_answer` for corpus or single scope.
- No packed evidence ends as `insufficient_evidence`.
- `_checkpoint`, `_generate`, `_validated` against the stored quotes, then `done(...)` -> `_finish`.
Every exception also ends in `_finish`: `_Stop` (cancelled or interrupted), `ServiceError` (clarification_required, request failed), and anything else (technical_error, failed). No exception leaves a request in `running`.