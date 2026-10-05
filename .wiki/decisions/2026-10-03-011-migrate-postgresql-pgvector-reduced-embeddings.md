---
scope: project
severity: preference
triggers: []
domain: ''
title: "Implement PostgreSQL persistence rehearsal and editable budget limits"
pr: 11
merged: 2026-10-03
branch: "migrate-postgresql-pgvector-reduced-embeddings"
---

# Implement PostgreSQL persistence rehearsal and editable budget limits

What. BidMate previously persisted all records and paid accounting in an earlier embedded database and served dense retrieval from NumPy. This draft introduces a pinned PostgreSQL/pgvector backend for the whole application, consistent resumable imports from the previous database, exact scoped vector search, …

Why. The owner's follow-up approves paid migration work and a $10 cap. Settings now edits the shared cumulative dollar limit with an attributed audit event, without resetting spending, unknown reservations or attempt prices. The approved live ledger update preserves all 108 historical attempts. …

Source. PR #11 · `migrate-postgresql-pgvector-reduced-embeddings`
