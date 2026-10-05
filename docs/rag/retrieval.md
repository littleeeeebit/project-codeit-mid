# Designing keyword and hybrid retrieval for Korean RFPs

Proposed first retriever: metadata filters and exact identifier lookup, followed by Korean BM25; add dense retrieval and reciprocal rank fusion as a controlled comparison. Institution names, requirement IDs, system acronyms, dates, amounts, and submission phrases carry business meaning. A semantically similar passage from the wrong RFP is an incorrect result.

## A lightweight Korean keyword baseline

[Kiwi's Python interface](https://github.com/bab2min/kiwipiepy) provides Korean tokenization and user-dictionary additions. [rank_bm25](https://github.com/dorianbrown/rank_bm25) accepts pretokenized documents, does not perform preprocessing itself, and requires the same preprocessing for queries. This combination supplies a small, local lexical baseline without an external search server.

Use one versioned analyzer for indexing and queries. Normalize searchable text to NFC, register a short domain dictionary, and preserve meaningful English acronyms. Start with nouns, useful verbs, Latin tokens, and numerals; inspect actual queries before expanding stopword rules. Do not discard “not,” mandatory/optional language, dates, or amounts merely because an analyzer treats them as low-information tokens.

Extract identifiers before morphological tokenization and index their normalized exact form separately. Preserve the source form. For example, a query for `SFR-001` must not match `SFR-0010`, and the same code in another document must not satisfy a selected-document request. Use deterministic title/institution aliases only for aliases supported by this corpus; broad LLM query expansion is not the baseline.

| Content | Treatment |
| --- | --- |
| Notice number and revision | Typed identity lookup, not fuzzy semantic matching |
| Institution and selected document | Explicit filter; resolve ambiguous names visibly |
| Requirement ID | Exact lookup within document/version; prefer detail over summary |
| Budget and deadline | Typed predicates and provenance; missing values remain unknown |
| Requirement prose | BM25 over Korean tokens and useful original compounds |
| Descriptive paraphrase | Dense search complements lexical matching |

Index meaningful heading text once alongside each chunk. Do not simulate field weighting by repeating titles many times. If title ranking becomes a measured issue, compare a separate document-title search channel or a field-aware search engine.

## Retrieval stages and starting parameters

1. Resolve the explicit document scope and deterministic filters. Do not ask the LLM to produce executable SQL. If interpretation is uncertain, present the filter or ask the user to select a document.
2. For an explicitly scoped requirement ID, retrieve that requirement's detailed block directly. If only its overview entry exists, say the detail is unavailable.
3. Retrieve the top 20 BM25 and top 20 dense candidates within the same filter scope. Persist and cache the question embedding by query, model, and dimensions.
4. Fuse the two ranked lists by chunk ID using `RRF(d) = sum(1 / (60 + rank_i(d)))`, where ranks start at one. A document absent from one list contributes zero there.
5. Rerank the best 20 fused candidates if the validated local reranker is enabled. Keep the explicit identity constraint throughout.
6. Pack up to six evidence units, expand only necessary parents/adjacent clauses, and enforce the evidence token limit after deduplication.

[Microsoft's RRF reference](https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking) explains combining ranks instead of unlike score scales. The constants and depths above are proposed settings. BM25 and cosine scores should not be averaged directly without a calibrated normalization experiment.

For cross-document comparisons, allocate evidence to each selected document. Global top-k can let one large RFP consume the entire context. For exhaustive lists, use the structured inventory: top-20 passage retrieval cannot promise every matching requirement.

## Alternatives already available

[Elasticsearch Nori](https://www.elastic.co/docs/reference/elasticsearch/plugins/analysis-nori-tokenizer) supports Korean compound decomposition and user dictionaries. If the team already operates Elasticsearch, trial `decompound_mode=mixed` and separate keyword fields for IDs. Otherwise, the installation and operation cost is unnecessary for the first 100 documents.

`text-embedding-3-large` at 1,536 dimensions serves today. The embedding matrix compares 13 models on the same chunks, fusion (Kiwi BM25 `keyword_first:60:1.0:6`), 50 candidates and 10 evidence units: ten local models (`BAAI/bge-m3`, `nlpai-lab/KURE-v1`, `dragonkue/BGE-m3-ko`, `Qwen/Qwen3-Embedding-0.6B`, `Snowflake/snowflake-arctic-embed-l-v2.0`, `dragonkue/snowflake-arctic-embed-l-v2.0-ko`, `ibm-granite/granite-embedding-278m-multilingual`, `google/embeddinggemma-300m`, `jinaai/jina-embeddings-v3`, `Alibaba-NLP/gte-multilingual-base`) and three API models (`text-embedding-3-large`@1536 on its existing vectors, `text-embedding-3-small`, `gemini-embedding-001`). Each model has a pinned revision and its documented query/document prefixes, and embeds every active chunk into its own set. Each gets a dense-only row and a hybrid row. [BGE-M3's model card](https://huggingface.co/BAAI/bge-m3) describes multilingual dense, sparse, and multi-vector representations; only its dense output is compared, and its learned sparse output is not equivalent to a Korean BM25 baseline. Every row can be activated; a local model then runs on the GPU in the server process.

## A fair comparison

| Run | What changes | What stays fixed |
| --- | --- | --- |
| K0 | Whitespace BM25 | Source elements, scope, queries, chunking |
| K1 | Kiwi BM25 with preserved IDs | Everything else in K0 |
| D | Dense-only retrieval | Same chunks and scope |
| H | Kiwi BM25 plus dense with RRF | Same evidence budget |
| HR | H plus local reranker | Same candidate pool and answer prompt |

First compare on the development set with no answer generation: evidence hit rate, multi-evidence coverage, nDCG@5, MRR, wrong-document rate, and p50/p95 latency. Separate exact-keyword, paraphrase, table, and comparison questions. Then evaluate answers for only the best two candidates under the same prompt and token ceiling.

Judge relevance against stable source spans. Overlapping chunks that repeat one fact do not count as multiple recovered facts. Evaluate recall before and after reranking to detect relevant evidence being demoted. Exact-code failures and wrong-document contamination deserve their own failure table even when the overall average improves.

The pipeline runs every variant and writes one table per matrix (`compare --matrix lexical|chunking|embedding|reranker`); the person reads the tables on 검증 › 실험 비교 and activates a row. Every run pins analyzer, dictionary, chunking, embedding model, index, and query-set versions.
