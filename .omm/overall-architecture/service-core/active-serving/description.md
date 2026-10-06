`active_serving(settings)` reads the app settings `active_run` and `active_index`. With no run it serves the keyword default (`kiwi_bm25`). Three conditions change what an activated run serves:
- If the run's `limits.corpus_route` differs from `retrieval.corpus_route_record()`, it falls back with `activated_corpus_route_requires_rerun`.
- If the run's embedding model is not allowed, it serves `kiwi_bm25`.
- If an HR run was scored under an older `EVAL_VERSION`, it keeps hybrid retrieval and drops the reranker.

`Resources.run_settings(cfg)` overrides the process settings with the run's measured limits, embedding model and dimensions, and reranker length, concurrency and precision, so serving reproduces what was evaluated. `describe_serving` produces the report wording.