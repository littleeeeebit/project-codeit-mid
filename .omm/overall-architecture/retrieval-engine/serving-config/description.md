`active_serving` reads app_settings `active_run` (JSON written by `evaluation.activate_decision`) and `active_index`. With no run, the keyword default (`settings.retrieval_mode`, fallback kiwi_bm25) serves. It falls back in three cases:
- The run's recorded `corpus_route` differs from the code's `corpus_route_record()`: serve the default and flag `activated_corpus_route_requires_rerun`.
- The embedding model is outside ALLOWED_EMBEDDING_MODELS: keyword.
- hybrid_rerank was promoted under an older EVAL_VERSION: hybrid without the reranker.
Every reader (requests, maintenance, experiments) goes through this function.