`_execute` opens one Langfuse trace seeded by the request ID and dispatches by mode:
- `metadata` and `inventory` are free: typed CSV facts and the requirement list from the index.
- `single`, `compare` and `corpus` go through `_paid_answer`.

`_paid_answer` runs these steps:
1. Refuses documents that are not parsed or not indexed (`ingestion_unavailable`).
2. `_checkpoint`.
3. `_conversation` loads up to 6 earlier turns of the same member and scope, plus up to 8 cited evidence units carried forward.
4. For a follow-up, `_rewrite`, which makes a paid `query_rewrite` call.
5. Preparation: `prepare_answer` (single or corpus) or `_prepare_compare` (one retrieval per side with single-document limits), or the frozen verifier run's evidence.
6. Returns `insufficient_evidence` with no paid call when there is no evidence.
7. `_metered_chat` (stage `generation`, streaming via `on_delta`).
8. `generation.validate_answer` against the allowed evidence IDs, the scoped documents and the stored quotes. In compare mode both documents must be represented.

Any exception is turned into a stored outcome by `done(...)` → `_finish`.