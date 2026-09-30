# Project purpose and dataset audit

The assignment in [프로젝트 개요.md](../../프로젝트%20개요.md) is an internal RAG system for 입찰메이트, a hypothetical B2G bid consulting startup. Consultants need to discover relevant RFPs, understand their requirements, identify the contracting organization and budget, and check submission rules. The supplied corpus supports historical discovery and grounded Q&A. It does not establish a live feed of currently open bids or automatic eligibility decisions for a particular company.

## What is present

Measurements below come from the local files on 2026-09-30. The [audit JSON](evidence/dataset-audit.json) contains file hashes and extraction outcomes.

| Item | Observed value | Consequence |
| --- | --- | --- |
| CSV records and original files | 100 records, 100 matching files | Audit all records rather than quietly sampling only easy files |
| Formats | 96 HWP, 4 PDF | HWP table extraction is the primary ingestion problem |
| CSV columns | 12, including metadata, summary, filename, and text | Keep typed metadata separate from retrieval text |
| CSV text | 384,353 characters total; minimum 89; median 2,716; maximum 18,335 | Nonempty text is not evidence of complete ingestion |
| Publication timestamps | 2021-10-08 to 2025-02-11 | Label the supplied corpus as historical; use an explicit as-of date |
| Missing notice number and revision | 18 each | Do not key documents by a nullable notice number |
| Missing amount | 1; additionally 6 zero amounts | Preserve raw values and flag zeros for review rather than calling them free projects |
| Missing bid start and closing time | 26 and 8 | Unknown dates remain unknown and cannot satisfy date filters |
| CSV encoding | UTF-8 with an existing BOM | Read with `utf-8-sig`; all new outputs use UTF-8 without BOM |
| CSV replacement characters | No U+FFFD found; Korean exists in every text field | This checks obvious decoding loss only, not missing content |

All 96 HWP files have the OLE compound-file signature. Their headers and preview streams were inspected during the audit; file extensions alone were not taken as format validation.

The complete structured parsing trial produced well-formed XML for 94 HWP files and text for all four PDFs, totaling 7,420,390 extracted characters. All 98 successful outputs were longer than their respective CSV text. Two HWP files failed: the 한국농어촌공사 AFSIS Cambodia project produced malformed XML, and the 대전대학교 MILE project encountered an illegal UTF-16 surrogate while parsing a style record. These results define the converter recovery queue; they do not certify the remaining files' visual/table fidelity.

The CSV text did not exactly match the inspected `PrvText` streams after whitespace normalization in any of the 96 HWP records. Its precise extraction provenance is unknown. Describe it as incomplete supplied text, not as a proven copy of HWP's preview stream.

## Duplicate originals and metadata conflicts

Two pairs of files are byte-identical even though their CSV associations differ:

- `국가과학기술지식정보서비스` and `한국한의학연구원` copies of the integrated information system RFP. The text names 한국한의학연구원, while the associated institution fields differ. Recorded closing times also differ: midnight versus 11:00 on 2024-06-11.
- `한국보건산업진흥원` and `BioIN` copies of the medical-device information system RFP. The text names 한국보건산업진흥원, while the associated institution/title/notice fields differ.

Reuse extraction and embeddings by original hash, but retain the separate metadata records and flag conflicts. A website/source label must not silently become the contracting institution. Review the original and notice provenance before canonicalizing an institution or deadline. Keep both copies and related questions in the same evaluation family; otherwise duplication creates leakage.

## Original PDFs prove that the CSV is incomplete

| PDF | Pages | CSV text characters | Original extraction characters |
| --- | --- | --- | --- |
| 고려대학교 portal and academic system | 297 | 2,450 | 232,655 |
| 서울시립대학교 longitudinal analysis system | 149 | 220 | 116,369 |
| 서울특별시 map information platform | 75 | 808 | 119,971 |
| 기초과학연구원 cryogenic system operations | 49 | 2,716 | 45,610 |

These are text lengths, not token counts or formally measured coverage percentages. PDF extraction used PyMuPDF across every page. No U+FFFD occurred in these outputs. The Seoul map PDF emitted syntax warnings while still yielding text; it needs visual and table checks before acceptance.

HWP samples show the same risk. The 한영대학 file has 1,799 CSV characters, 19,039 characters through `hwp5txt`, and 45,359 text characters through structured XML inspection. Its XML contains 110 table controls and 1,545 cells, including layout tables. The 국방과학연구소 records-management file has 6,828 CSV characters and 58,538 XML text characters. Plain conversion emits `<표>` placeholders where important table content should be.

## Information that must survive

Preserve notice number, revision, institution, title, publication and closing timestamps, source filename and hash, requirement identifiers such as `SFR-001`, monetary units and VAT status, submission method, and evidence locations. `SFR-001` occurs in many RFPs; it is meaningful only together with document identity.

Use an internal stable `doc_id` and a separate source hash/version. Keep the original CSV metadata and original document wording as distinct evidence sources. The provided summaries have no recorded generation or review provenance, so use them for browsing hints, not gold answers or authoritative factual citations.

Amounts should be exact integer KRW when they are valid amounts; preserve source strings and ambiguity. Normalize valid revisions such as `0.0` to revision zero without treating missing revisions as zero. Parse Korean local timestamps with an explicit timezone; keep date-only values date-only. Never substitute publication time for a missing closing time.

## Scope of the useful product

A consultant should be able to filter and search projects, select one or a few RFPs, ask about concrete requirements, and inspect the supporting original text. Across-document comparisons must identify each document and show missing facts per document. A current opportunity recommendation requires fresh notices and customer capability data that are not supplied here.

No application code or prior stack was found in the initial inventory. The existing recorded decision named `start` contains no implementation rationale to preserve or reverse.
