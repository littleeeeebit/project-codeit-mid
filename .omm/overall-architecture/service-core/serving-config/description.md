`active_serving(settings)` reads `app_settings.active_run` and `active_index` on every `Resources.serving()` call. With no activation, it serves `settings.retrieval_mode` over the active index. It degrades a stored activation in three cases:
- The corpus-route rule changed since the run was measured: it serves the keyword default with `fallback_reason: activated_corpus_route_requires_rerun`.
- The embedding model is not allowed: it serves kiwi_bm25.
- A hybrid_rerank run comes from an older EVAL_VERSION: it serves hybrid without the reranker.
`Resources.run_settings` overlays the run's limits, embedding model and dimensions, and reranker parameters on Settings, so serving reproduces what was measured.