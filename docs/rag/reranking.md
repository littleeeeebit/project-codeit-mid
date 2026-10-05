# Selecting and validating a Korean RFP reranker

The reranker table compares eight local rerankers on the serving hybrid's frozen pool (50 candidates), each in two modes. Whole-list mode reorders every candidate and repeats the failure measured on 2026-10-04. Below-the-head mode keeps the BM25 head (top 6) in place and reorders only what follows. The no-reranker bypass serves until the person activates a reranker row. Nothing here claims Korean procurement superiority.

## Candidates and constraints

| Option | Established capabilities | Project choice |
| --- | --- | --- |
| No reranker | Retains BM25/RRF ranking, no reranking model cost | Baseline row and fallback |
| `BAAI/bge-reranker-v2-m3`, `dragonkue/bge-reranker-v2-m3-ko` | Multilingual cross-encoder and its Korean fine-tune; Apache-2.0 | Compared |
| `BAAI/bge-reranker-v2-gemma`, `BAAI/bge-reranker-v2-minicpm-layerwise` | LLM-based rerankers scoring a Yes token; the layerwise model's layer cutoff is recorded | Compared |
| `Qwen/Qwen3-Reranker-0.6B`, `mixedbread-ai/mxbai-rerank-base-v2`, `mixedbread-ai/mxbai-rerank-large-v2` | Causal-LM rerankers with a yes/no or relevance-token score | Compared |
| `Alibaba-NLP/gte-multilingual-reranker-base` | Multilingual cross-encoder | Compared |
| Paid hosted reranker or LLM judging passages | Extra calls, credentials, and spend | Not compared: the owner selected local rerankers only |

Each row records the pinned revision, licence, precision, input length, size, cold load and peak VRAM. A model that fails to load, runs out of memory or crashes stays in the table with its reason.

The [BGE reranker model card](https://huggingface.co/BAAI/bge-reranker-v2-m3) documents query-passage scoring and a sigmoid option. Sigmoid-normalized scores are not calibrated probabilities of a correct answer. It also describes FP16 as a speed/precision tradeoff; do not assume FP16 is appropriate for every CPU environment.

The [Jina model card](https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual) describes a 1,024-token window and sliding-window handling of longer text. Its noncommercial license and model-specific execution dependencies differ from BGE's; similar multilingual labels do not make them interchangeable.

`BAAI/bge-m3` is an embedding model with dense/sparse/multi-vector modes. `BAAI/bge-reranker-v2-m3` is a different model used to score query-passage pairs. Avoid confusing their names or reporting embedding cosine scoring as cross-encoder reranking.

## Integration rules

Run ranking on child chunks, before expanding to larger parents. Include the document title, requirement ID, and necessary table headers in the passage. Measure length with the reranker's tokenizer; OpenAI token counts cannot guarantee that a passage fits another model's input limit.

A generic truncation from the end can drop a submission condition or exception. When an oversized candidate needs splitting, use source-aware windows and report how window scores are combined. Start by designing bounded chunks so normal cases do not need that extra mechanism.

Preserve document and version constraints. An explicit requirement-code request must not lose its correct detailed block to a similarly phrased requirement from another RFP. Deduplicate candidates from overlapping chunks, and keep summary and detail types distinct. Reranking cannot recover evidence missing from the candidate pool.

Local inference removes API charges for this stage, not runtime, download, memory, or electricity costs. Warm the model once in the shared process and measure both cold start and warmed calls. CPU/GPU availability and performance have not been established for the team's deployment.

## The acceptance experiment

`compare --matrix reranker` reranks the serving hybrid's frozen pool, with the same candidates, development labels and evidence-token budget for every row. Columns: nDCG@5, complete support, recall@20 before and after, critical failures against the serving hybrid and against K1, truncated pairs, warm p95 alone and under six users, and the existing gate result. Scores are cached per model, query and passage, so a rerun reuses them.

The gate column applies the existing condition: development nDCG@5 at least 0.03 above the serving hybrid, no new critical exact-ID or wrong-document regression, and no more than one second of added warm p95 latency at six users. It informs the person's choice and blocks nothing. Candidate depth stays at the serving 50; depth sweeps are not part of this comparison.

A model that fails to load or crashes serves nothing: the validated RRF/BM25 path keeps serving and the trace records the fallback. No paid LLM reranking replaces it.
