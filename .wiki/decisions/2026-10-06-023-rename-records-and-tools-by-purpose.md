---
scope: project
severity: preference
triggers: []
domain: ''
title: "Categorize records and tools by purpose: handoff/ → docs/history/, and tools/ into export, retrieval, infra, and verification."
pr: 23
merged: 2026-10-06
branch: "rename-records-and-tools-by-purpose"
---

# Categorize records and tools by purpose: handoff/ → docs/history/, and tools/ into export, retrieval, infra, and verification.

What. Renamed folders and files named by phase numbers (phase2, phase4, etc.) to names that describe their content. `src/` Structural separation will be done separately in the next PR.

Why. Records (`handoff/` → `docs/history/<주제>/`) - `handoff/phase2` → `docs/history/retrieval-selection-kit/` (`.gitignore` also moved, `.private`·`local-inputs` continue to be ignored) - `handoff/phase3` → `docs/history/workflow-acceptance/` - `handoff/phase4` → `docs/history/release-walkthrough/` - `handoff/postgresql-pgvector` → `docs/history/postgresql-migration/` - `handoff/frontend-redesign-shell.spec. …

Source. PR #23 · `rename-records-and-tools-by-purpose`
