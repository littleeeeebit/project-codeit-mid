# Chunking Korean RFP requirements and tables

Proposed default: split complete parsed documents by section, requirement block, and table row groups before applying a token ceiling. Start with roughly 300 to 700 tokens per searchable unit, an 800-token hard ceiling including its searchable heading, and at most 64 tokens of overlap for oversized prose. These are experiment settings, not research-backed universal optima.

The reusable idea is element-based chunking. [Unstructured's documentation](https://docs.unstructured.io/open-source/core-functionality/chunking) describes keeping document elements and section boundaries together, isolating tables, and splitting oversized elements. Its size options count characters, so they cannot be copied as token limits. Adopt the pattern without assuming its parser directly solves binary HWP.

## Units for this corpus

| Source content | Searchable unit | Preserve with it |
| --- | --- | --- |
| Project overview | One subsection or a short consecutive paragraph group | Document title, institution, section path |
| Detailed requirement | Identifier, name, description, exceptions, deliverables | Document identity and the actual requirement identifier |
| Requirement summary table | A separate overview unit | Mark it as a summary; it is not the detailed requirement |
| Budget or timetable | Rows grouped by the fact they express | Column names, currency/unit, VAT note, date type |
| Submission and eligibility | Rule together with its conditions and exceptions | Mandatory/optional wording, deadline, responsible organization |
| Annex or form | Separate section | Form identity, applicability, source location |

Do not let a generic splitter detach “VAT included,” “except when,” or a table header from its amount or rule. Never merge chunks across documents or source revisions. Requirement codes may have irregular source spelling, such as `SFR-09`; preserve the original and treat any searchable alias as an alias, not a corrected source fact.

## Table representation

Store table identity, row and column coordinates, merged-cell spans, and raw text. Build searchable rows as header-value pairs, carrying required spanning headers forward. Split a long table into row groups with repeated headers. If a requirement uses several rows, group those rows before splitting. Do not flatten every table to an unstructured paragraph: RFP files also use tables to lay out normal prose, so distinguish layout cells from actual tabular records during inspection.

A source citation must resolve to the original table/cell or PDF page region. A generated Markdown table is a presentation format, not a replacement for that provenance.

## Search small and expand selectively

Retrieve requirement-sized children. Expand only the selected child's parent subsection or neighboring clause if the answer needs it. [LlamaIndex's recursive retriever example](https://developers.llamaindex.ai/python/framework/integrations/retrievers/recursive_retriever_nodes/) demonstrates retrieving smaller nodes and following references to larger nodes. A parent ID and a lookup suffice for this project's first version; a recursive retrieval framework is optional.

Start with a 3,000-token evidence budget and a 5,000-token hard evidence ceiling. Deduplicate overlapping source spans after expansion, preserve complete cited facts, then count the assembled prompt. A larger parent must not silently bypass the budget. See [usage accounting](budget.md).

## The comparison

Freeze parser output and the evaluation queries. `compare --matrix chunking` builds the structural splitter and fixed token windows at `256/32`, `512/64` and `800/96` size/overlap as non-serving indexes, scores each with K1 on one population, and writes one table: chunk count, duplicated tokens, nDCG@5, complete support, qualifier losses, critical failures and latency. No paid generation runs. The person reads the table; activating a row's run is what switches the serving index. Keep the generator and evidence token budget identical when evaluating the finalists.

Store gold evidence as document version plus source spans, not only current chunk IDs. Rechunking otherwise changes the target being measured. Include long requirements, cross-page tables, summary/detail duplicates, and questions needing an exception found beside the retrieved clause.

Choose the smallest representation that retains the needed evidence. Defer LLM semantic chunking and per-chunk generated summaries unless structural chunks show a specific failure they can fix; both consume money and introduce another source of factual drift.
