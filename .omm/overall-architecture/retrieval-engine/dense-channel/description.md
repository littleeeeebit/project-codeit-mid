`dense.query_vector` looks up `embedding_payloads` by payload hash (the normalized text, model, dimensions and 'query'). On a miss:
- A local model embeds the query on the GPU, for free.
- An OpenAI model pays only when `allow_paid` is set and a request id exists, through `metered_embed` (reserve, mark_dispatching with the request's guard, embed, then settle or unknown). The vector is then cached.
- Gemini uses its own capped ledger.
`PgDenseIndex.load` verifies the set row by row: model, dimensions, policy, checksum, unit norm, and a chunk order identical to the keyword index. `search` runs EXACT_SQL cosine (`<=>`) restricted to the allowed `row_order`, or HNSW only when the run chose `dense_search: hnsw`.