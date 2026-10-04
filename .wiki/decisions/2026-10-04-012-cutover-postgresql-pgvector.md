---
scope: project
severity: preference
triggers: []
domain: ''
title: "Measure fixed-1536 acceptance before PostgreSQL cutover"
pr: 12
merged: 2026-10-04
branch: "cutover-postgresql-pgvector"
---

# Measure fixed-1536 acceptance before PostgreSQL cutover

What. The PostgreSQL migration lacked a measured acceptance result for the owner-fixed 1,536-dimensional candidate. Add a cache-aware offline comparison command that requires frozen source-reviewed development labels, preserves every chunk association when deduplica …

Why. Actual execution used 55 frozen pilot questions, ten bounded native-reference batches and 110 query calls through the sole live ledger. All 120 embedding attempts settled for $0.117928; label drafting cost $0.003260. Both candidate channels stayed within the average-loss thresholds but introduced critical support failures. …

Source. PR #12 · `cutover-postgresql-pgvector`
