---
scope: project
severity: preference
triggers: []
domain: ''
title: "Review App: Paid execution that runs only after comparing answer generation/OCR candidates and estimating approved costs"
pr: 37
merged: 2026-10-10
branch: "review-app-generation-ocr"
---

# Review App: Paid execution that runs only after comparing answer generation/OCR candidates and estimating approved costs

What. In the Review App, we have enabled the comparison of answer generation and OCR candidates, following retriever and chunking. Cost-incurring executions stop after generating a price estimate within the CLI runner and proceed only after human approval. Decisions are passed in the same file+prompt format as retriever and chunking.

Why. (the PR body has no reason section; the grounds for this decision were not recorded)

Source. PR #37 · `review-app-generation-ocr`
