`_checkpoint` runs first, because the query embedding may be the first paid stage. The path then depends on the request:
- Frozen verifier run: `_frozen_prep` reprices the stored evidence. It ends `clarification_required` if the new estimate exceeds the consented one.
- compare: `_prepare_compare`.
- single: `prepare_answer(allow_paid=True)`.

For `compare` and `single`, a dense mode may reserve, dispatch and settle an `embedding` attempt (`dense.query_vector` → `metered_embed`, guarded by `_dispatch_guard`). `_priced` builds the exact messages, counts tokens and estimates the maximum, adding a query-embedding estimate on a cache miss. Failures end `technical_error`. Empty evidence ends `insufficient_evidence` with `not_found_in_context` per document.