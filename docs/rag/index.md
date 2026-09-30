# RFP assistant research and implementation guide

This wiki prepares the 입찰메이트 team to build a usable internal RFP assistant in two to three days, then improve it over more than three weeks with a shared OpenAI allowance of $20 for six people. It contains research and proposed implementation choices, not an implemented application or measured retrieval results. Sources and prices were checked on 2026-09-30.

Start with the [project and dataset audit](project-and-data.md). The main finding is that the CSV text is incomplete, and a plain HWP text converter can also omit tables. Improving retrieval cannot recover content that was never indexed.

For the complete implementation sequence, read the [end-to-end overview](../plan/end-to-end/0-overview.md). It links the shared implementation contracts and five detailed assignments covering the guarded slice, corpus/retrieval, usable workflows, evaluation/release, and subsequent team operation. Each assignment specifies file responsibilities, ordered work, verification and handoff gates.

## The requested research topics

| Topic | Document | Question it resolves |
| --- | --- | --- |
| Chunking | [Chunking](chunking.md) | How to keep requirements, table headers, exceptions, and evidence locations together |
| Preprocessing | [Korean HWP and PDF preprocessing](preprocessing.md) | How to recover complete text and tables without corrupting Korean |
| Similar domain and architecture | [Procurement RAG architecture](architecture.md) | Which existing patterns fit RFP discovery and document Q&A |
| LLM generated golden dataset | [Golden dataset construction](golden-dataset.md) | How to make evidence-backed, difficult, independently reviewed examples |
| Retriever | [Keyword and hybrid retrieval](retrieval.md) | How to build Korean keyword search and compare it fairly with dense search |
| Prompt engineering | [Grounded RAG prompts](prompts.md) | How to answer with verifiable citations and handle missing or conflicting evidence |
| Reranker | [Reranker selection](reranking.md) | Which local multilingual model to trial and when to bypass it |
| Evaluation | [Evaluation and release criteria](evaluation.md) | How to measure retrieval, answer quality, abstention, and cost separately |
| Frontend | [Consultant and verification interfaces](frontend.md) | How to separate everyday work from controlled experiments |

The [shared budget and usage ledger](budget.md) is a cross-cutting requirement. The [delivery plan](delivery-plan.md) describes dependencies, six workstreams, and completion checks. The [source register](sources.md) records what each external source establishes and where it falls short.

## Proposed first version

Use a single shared Python service, SQLite for documents and the spending ledger, Korean BM25 with Kiwi, and a persisted normalized embedding matrix for dense search. Test `text-embedding-3-small` against the keyword baseline; start answer generation with `gpt-4o-mini`. Trial `BAAI/bge-reranker-v2-m3` locally and keep an explicit bypass when it misses the quality or latency gate. These choices remain subject to the domain evaluation; no model is claimed to be best for this dataset.

Use two Streamlit entry points with different purposes and access controls: consultant search and grounded answers, and team verification with frozen experiments. Both call the same pipeline and spending gateway. Metadata-only answers and ordinary document search should incur no generation charges.

The smallest useful path is full ingestion, filtered keyword search, source-backed answers, and a shared budget ledger. Hybrid retrieval adds a useful comparison without requiring a hosted vector database. Start with BM25 when the dense index is unavailable; report that mode visibly.

## Evidence and unresolved measurements

The [machine-readable audit](evidence/dataset-audit.json) records source hashes, metadata gaps, PDF extraction sizes, and HWP inspection results. Extraction success is a syntax and content-volume observation, not proof of table fidelity. Before implementation, read the preprocessing acceptance checks and inspect representative originals visually.

Hardware speed, model access for the academy key, remaining credit, prior spending, and provider administrative permissions have not been verified. No calls were made to the team's OpenAI key for this research. The plan uses a configurable 28-day horizon as a conservative example; the actual project end date must set the pacing calculation.
