---
scope: project
severity: preference
triggers: []
domain: ''
title: "Comparison of Phase 2 Corpus Coverage and Search Methods"
pr: 4
merged: 2026-10-01
branch: "feat/phase2-corpus-retrieval"
---

# Comparison of Phase 2 Corpus Coverage and Search Methods

What. Implemented Phase 2 of `docs/plan/end-to-end/2-corpus-and-retrieval.md`. The command table of the plan was created as a CLI as is, and the usage and file format are in the "Phase 2" section of the README.

Why. Collection (`ingest --profile all`): If the original text, parser revision, Hancom printed copy, OCR cache, and HWP converter remain the same, extraction is reused (enforcement via `--force`). The time taken per file and automatic diagnostics (`short_output`, `no_tables`, `thin_tail`, `blank_pages`, etc.) are left in `.runtime/reports/ingest-*.json`. Diagnostics do not mean passing the review. Even if one file dies, it is recorded as `error` and proceeds, and the exit code is 1 - Review Record (`import-reviews`): Verifies extraction revision, element location, reviewer, and inspection items. If even one is wrong, nothing is entered ...

Source. PR #4 · `feat/phase2-corpus-retrieval`
