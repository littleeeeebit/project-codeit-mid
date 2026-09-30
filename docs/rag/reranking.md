# Selecting and validating a Korean RFP reranker

Proposed first candidate: run `BAAI/bge-reranker-v2-m3` locally on the top 20 fused candidates, with an explicit no-reranker fallback. Deploy it only after it improves the RFP development results within the latency and memory budget. This is a candidate selection, not a claim of Korean procurement superiority.

## Candidates and constraints

| Option | Established capabilities | Project choice |
| --- | --- | --- |
| No reranker | Retains BM25/RRF ranking, no reranking model cost | Mandatory baseline and fallback |
| `BAAI/bge-reranker-v2-m3` | Publisher describes a multilingual cross-encoder; Apache-2.0 model license | First local trial |
| `jinaai/jina-reranker-v2-base-multilingual` | Publisher describes multilingual reranking, sliding windows, and CC-BY-NC-4.0 licensing | Research comparison only after checking applicable use terms |
| Paid hosted reranker or LLM judging passages | Extra calls, credentials, and spend | Defer while the $20 allowance is shared |

The [BGE reranker model card](https://huggingface.co/BAAI/bge-reranker-v2-m3) documents query-passage scoring and a sigmoid option. Sigmoid-normalized scores are not calibrated probabilities of a correct answer. It also describes FP16 as a speed/precision tradeoff; do not assume FP16 is appropriate for every CPU environment.

The [Jina model card](https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual) describes a 1,024-token window and sliding-window handling of longer text. Its noncommercial license and model-specific execution dependencies differ from BGE's; similar multilingual labels do not make them interchangeable.

`BAAI/bge-m3` is an embedding model with dense/sparse/multi-vector modes. `BAAI/bge-reranker-v2-m3` is a different model used to score query-passage pairs. Avoid confusing their names or reporting embedding cosine scoring as cross-encoder reranking.

## Integration rules

Run ranking on child chunks, before expanding to larger parents. Include the document title, requirement ID, and necessary table headers in the passage. Measure length with the reranker's tokenizer; OpenAI token counts cannot guarantee that a passage fits another model's input limit.

A generic truncation from the end can drop a submission condition or exception. When an oversized candidate needs splitting, use source-aware windows and report how window scores are combined. Start by designing bounded chunks so normal cases do not need that extra mechanism.

Preserve document and version constraints. An explicit requirement-code request must not lose its correct detailed block to a similarly phrased requirement from another RFP. Deduplicate candidates from overlapping chunks, and keep summary and detail types distinct. Reranking cannot recover evidence missing from the candidate pool.

Local inference removes API charges for this stage, not runtime, download, memory, or electricity costs. Warm the model once in the shared process and measure both cold start and warmed calls. CPU/GPU availability and performance have not been established for the team's deployment.

## The acceptance experiment

Compare hybrid retrieval with and without the reranker on the same candidate pool, development labels, and final evidence-token budget. Record evidence recall@20 before reranking, hit rate/nDCG@5 after it, complete evidence coverage for multi-hop cases, and end-to-end grounded answer correctness for the finalists.

Proposed promotion condition: improve development nDCG@5 by at least 0.03, cause no new critical exact-ID or wrong-document regressions, and add no more than one second to warmed p95 latency at the planned six-user load. These are product targets to revise with actual hardware measurements, not published performance numbers. Report uncertainty if a small sample makes the gain unstable.

Measure candidate counts of 10 and 20 before testing larger pools. Increasing candidate depth may improve recall but raises runtime and can erase a context-token saving. The release decision must consider quality, latency, and actual answer cost together.

If the model fails to load or misses the latency gate, serve the validated RRF/BM25 path and record the fallback in the trace. Do not replace it with paid LLM reranking automatically. Revisit optimization or a different local model only when there is a measured failure worth fixing.
