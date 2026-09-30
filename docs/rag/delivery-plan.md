# Delivering a usable end to end RFP assistant in two to three days

Deliver the complete path from original file to reviewed source evidence and a consultant answer, with a shared spending guard. End to end includes ingestion failures, unknown fields, evidence navigation, and recorded costs. It does not require every advanced RAG component to be enabled before the first usable version.

This is a proposed team work plan. No application has been implemented and no team work has been dispatched.

## Six workstreams with concrete outputs

| Workstream | Deliverable | Depends on |
| --- | --- | --- |
| Ingestion | Versioned source elements, table representation, 100-file status manifest, failed-file fallback | [Dataset audit](project-and-data.md), [preprocessing](preprocessing.md) |
| Retrieval | Scoped Kiwi BM25, exact requirement lookup, dense comparison and persisted indexes | Reviewed elements and [chunk contract](chunking.md) |
| Generation and budget | One paid-call gateway, reservation ledger, prompt/output contract, citation checks | Stable source IDs and verified model access/prices |
| Consultant interface | Search, scope selection, answer/evidence workflow, shared usage display | Shared retrieval/generation contracts |
| Verification interface | Retrieval-only traces, frozen run comparisons, controlled generation actions | Shared pipeline and ledger |
| Golden set and evaluation | Reviewed source-span labels, family split, pilot and final evaluation | Reliable extraction and [question taxonomy](golden-dataset.md) |

Agree on `doc_id`, source version/hash, element/evidence ID, chunk ID, ingestion state, request/attempt ID, and usage fields before connecting these pieces. Avoid six independent parsers, retrievers, or spending counters.

## First day

Timebox the structured HWP converter trial and original-table checks to the first two hours. Reuse the audit finding: 94 HWP files produced well-formed XML, two did not, and all four PDFs produced text. Inspect representative tables rather than treating these counts as content-fidelity passes. Route the two failures through a verified conversion fallback or mark them unavailable with their reason.

Create the shared metadata/element store and status manifest. Group identical original hashes for extraction and evaluation, while retaining the separate CSV records and their provenance conflicts. Build scoped Korean BM25 and requirement-code lookup. Make a search screen that already lets a consultant find and read evidence without paid generation.

Implement the shared spending gateway before the first academy-key call. Record prior spending, actual project end date, and the operational cap. Verify model access with one bounded call only when implementation begins. Prepare 24 independently reviewed pilot questions from different sections and document families.

Day-one completion: from a supported HWP and a PDF, search a requirement beyond the opening pages, open its source, and run one metered grounded answer. Include a visible failure-state example. This is the first end-to-end milestone, not just a backend import demo.

## Second day

Build the shared persisted embedding index once and compare dense-only and hybrid retrieval against BM25 using the same pilot. Trial local BGE reranking only after candidate recall is visible. Complete source citations, unknown/conflict/ambiguity handling, and the separated verification workflow.

Exercise six concurrent sessions, budget reservations, interrupted calls, duplicate settlement, restarts, and cap exhaustion. Review hard source facts and misleading metadata. Expand the reviewed golden set; run answer generation for the two development finalists only.

Two-day delivery can use validated BM25 plus one grounded generation call if dense retrieval or reranking fails its gate. Keep comparison artifacts and clearly label the active mode. The full ingestion-to-answer path, citations, budget protections, and both interface purposes remain in scope.

## Third day when available

Finish recovering and reviewing failed originals, improve measured retrieval failures, complete the 120-row gold set and sealed evaluation, and run one final integrated usability check. Record model/configuration versions, actual metrics, costs, and unresolved failures for the mentor presentation.

If the reviewed set or conversion fallback is unfinished, ship the usable supported path with its exact coverage and pilot results. Do not label unreviewed synthetic examples “gold” or present a partial evaluation as final reliability evidence.

## Keep the remaining weeks affordable

Fix errors in this order: extraction/metadata provenance, wrong scope or identifier handling, missing evidence, context packing, unsupported answer claims, then model changes. Each step fixes a concrete failure and makes later comparisons meaningful.

Reindex only changed unique sources and reuse embeddings across team members and experiments. Store traces and corrections, add difficult regression questions from real consultant use, and run retrieval-only experiments first. Audit spending and provider reconciliation on a regular team schedule without rerunning all paid evaluation metrics every day.

Defer fine-tuning, agents, GraphRAG, hosted vector infrastructure, and separate frontend frameworks. Add them only when a measured quality, runtime, or team workflow problem survives the simpler route.

## Completion checklist

- All 100 records have explicit ingestion and metadata-quality status; supported originals have reviewed tables and late-document content.
- A consultant can search, select scope, ask, see unknowns/conflicts, and open the correct original evidence.
- A verifier can reproduce retrieval traces and compare frozen runs without unintended paid calls.
- Every paid stage and retry is attributed to the shared ledger; cap and concurrent-request checks pass.
- Model access, prices, generation limits, and the actual project horizon are recorded before spending.
- Evaluation reports its source families, question types, sample counts, uncertainty, costs, and failure examples.
- Generated artifacts and documents are UTF-8 without BOM, and final validation is run after the last change.
