---
scope: project
severity: preference
triggers: []
domain: ''
title: "BidMate runs only on PostgreSQL 18.6 + pgvector 0.8.6 and text-embedding-3-large fixed at 1,536 dimensions. All records and billing ledgers are stored in PostgreSQL, and no code path uses any other database. Hybrid search via pgvector provides answers without losing keyword-only search responses for questions, and users can query across all documents and obtain the correct passages."
pr: 13
merged: 2026-10-04
branch: ""
---

# BidMate runs only on PostgreSQL 18.6 + pgvector 0.8.6 and text-embedding-3-large fixed at 1,536 dimensions. All records and billing ledgers are stored in PostgreSQL, and no code path uses any other database. Hybrid search via pgvector provides answers without losing keyword-only search responses for questions, and users can query across all documents and obtain the correct passages.

What. Switched BidMate exclusively to PostgreSQL 18.6 + pgvector 0.8.6 and deleted all code for the previous database. Embeddings remain `text-embedding-3-large` at 1,536 dimensions. Added an 'entire document' scope to querying.

Why. | Item | Result | | --- | --- | | Final Import | After stopping UI and writes, the final snapshot of the previous database (`ca299be9…`) was imported into `bidmate_app` and verified. 27 tables and 674,049 rows match the original, and all reference artifact hashes also match. Past attempts and cumulative usage remain the same, and there are 0 unconfirmed charges. | | Vector Equivalence | 18,983 active chunk 1,536-dimension vectors are byte-for-byte identical to the verified preparation set. The `29f261abafeb1f8c` keyword index was enabled to resolve the `index_outdated` warning. | | HNSW | Even after increasing `ef_search` to 400, the recall@20 for scoped queries is 0. …

Source. PR #13
