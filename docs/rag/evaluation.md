# Evaluating RFP retrieval and answers without easy-data inflation

Proposed evaluation: freeze a reviewed, difficult question set and separately measure ingestion, metadata lookup, retrieval, generated claims, abstention, latency, and dollars. A fluent answer or one aggregate score cannot reveal which stage failed. Proposed thresholds below are release targets, not results already achieved.

[Ragas](https://arxiv.org/abs/2309.15217) separates aspects of retrieved context and generated answers. [ARES](https://arxiv.org/abs/2311.09476) evaluates context relevance, answer faithfulness, and answer relevance using automated judging calibrated with human annotations. These sources motivate stage-specific measures; neither makes an uncalibrated LLM judge a substitute for reviewed RFP evidence.

## Measurements by stage

| Stage | Measure | Important definition |
| --- | --- | --- |
| Ingestion | 100-file status inventory; checked requirement/table coverage | A parsed XML file is not automatically a faithful table |
| Metadata | Exact field and filter correctness | Missing values, VAT, revisions, timezone, and dates keep their meaning |
| Single-evidence retrieval | Hit@k, nDCG@5, MRR | A result must contain relevant source evidence, not merely the right title |
| Multi-evidence retrieval | Evidence recall@k and complete coverage@k | Measure recovered required evidence units and whether all needed units are present |
| Identity | Wrong-document/version rate | Repeated requirement codes cannot cross the selected scope |
| Answer | Correct required claims; unsupported-claim rate | Preserve amount, unit, condition, exception, and mandatory wording |
| Citations | Precision, claim coverage, and source-link validity | Valid links and semantic support are separate checks |
| Negative/ambiguous cases | Correct abstention, false answer rate, unnecessary refusal | Include truly absent, ambiguous, conflicting, and unavailable evidence |
| Operations | p50/p95 retrieval and full-answer latency, failure rate | Report warm/cold conditions, sample count, and concurrency |
| Spending | Actual input/output/embedding tokens and USD per attempt/question | Include retries, judge calls, and indexing amortization |

For a single required fact, hit@k is whether any valid supporting passage is present. For several required facts, evidence recall@k is the number recovered divided by the number required; complete coverage@k requires all of them. Overlapping chunks do not create additional independent evidence. Map results to stable source spans so changed chunk sizes do not change the ground truth.

Use graded relevance for nDCG when labels justify it: complete support, partial useful support, and irrelevant. State grades and aggregation. Exclude unanswerable and metadata-only questions from passage recall denominators, and report them in their own strata. Inspect retrieved evidence before calling an answer-generation failure a retrieval failure.

## Avoid a deceptively easy benchmark

Follow the [golden dataset plan](golden-dataset.md): 120 reviewed questions, half sealed for the final test, family-level separation, mostly paraphrase/table/code/exception/comparison cases. The audit found two pairs of byte-identical originals with different associated metadata; group them before splitting and use the conflicts as realistic tests.

Do not select questions only from opening pages, only from successful parser outputs, or only from passages your chosen retriever already returns. Include the two converter-failure documents as ingestion-recovery cases, without calling their missing extraction “unanswerable in the source.” Reject synthetic questions that accidentally supply the answer or an exact unique chunk heading.

Pilot with 24 reviewed development questions to diagnose the pipeline. Expand before making reliability claims. Report per-type counts and numerators, not just percentages. For a 60-question sealed set, one question changes the score by about 1.67 percentage points; report Wilson confidence intervals for binary rates and bootstrap intervals grouped by document family for ranked metrics when practical. Tiny strata warrant caution rather than a strong leaderboard claim.

## Comparison procedure

Pipelines run every variant and AI reviewers approve gold rows, development and sealed. A person chooses from the comparison tables and activates. Maintenance is one command or button with no schedule (see the [operating rule](../plan/end-to-end/0-overview.md#operating-rule-pipelines-run-reviewers-approve-a-person-picks)).

1. Freeze source versions, parsed evidence labels, development queries, scope, and token limits.
2. Run whitespace BM25, Kiwi BM25, dense-only, and hybrid retrieval without generation. Use one set of cached query embeddings for applicable runs. `compare --matrix <name>` runs every variant of a declared matrix over the development, needle and whole-corpus sets and writes one table.
3. Compare chunk settings, embedding models and then reranking as separate changes. Only one thing changes per row: fusion, chunking and limits stay fixed while a model changes, and rerankers score the serving hybrid's frozen pool.
4. Generate answers only for the two development finalists. Keep generation model, prompt, context-token ceiling, and output cap fixed.
5. Review critical facts deterministically where possible and with an AI reviewer where context matters. Inspect amount units, VAT, date type, and negation; string similarity alone is inadequate.
6. Use LLM judges only for a bounded sample of qualitative errors, blind to run names. Check their agreement with the reviewed reference verdicts and keep disputed cases visible. Do not judge every configuration with many paid metrics by default.
7. The person reads the tables and activates a row. Gates are columns, not automatic decisions. Evaluate the sealed set once after that choice, report failures and limitations, and create new regression cases after release without retuning on the sealed answers.

The test corpus remains searchable. Holding out questions and labels prevents benchmark tuning; omitting the documents would test a different task. Test questions/answers must not appear as retrievable content or few-shot prompt examples.

## Proposed release gates

Require all 100 files to have a status, with fully checked supported documents and clearly labeled unresolved failures. Do not market incomplete originals as completely searchable. All citation targets must resolve to their authorized source version. Selected-document leakage must be zero in the reviewed test.

Aim for evidence hit@20 of at least 90% on answerable single-evidence cases, complete coverage@20 of at least 80% on multi-evidence cases, at least 90% correctness on reviewed required claims, and at least 95% citation precision. Require no observed critical misstatement of a deadline, budget, mandatory condition, or institution. Report actual denominators and uncertainty; these targets do not establish production safety by themselves.

Aim for at least 90% correct handling of negative/ambiguous cases and inspect every false answer. A verified missing field should be marked unknown; an ingestion failure should be marked unavailable; an ambiguous scope should prompt clarification. These are different outcomes even when each avoids an invented answer.

Set hardware-dependent targets of warmed retrieval p95 below two seconds and full-answer p95 below fifteen seconds at six concurrent users. Measure and revise them before promising an SLA. Prefer the cheaper/faster baseline if a more complex pipeline has no demonstrated quality gain.

Budget gate: all paid paths use the shared ledger, reservations cannot exceed the operational cap, and the UI displays settled estimates versus pending/unknown costs correctly. A paid comparison step (an API embedding of the corpus or its queries) runs only under a priced estimate the person approved. The comparison tables on 검증 › 실험 비교 keep run versions, scores, latency, costs and per-question failures side by side for the mentor presentation.
