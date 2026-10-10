---
scope: project
severity: preference
triggers: []
domain: ''
title: "A local review app that compares retriever/chunking candidate refs with the same input and records the decisions."
pr: 36
merged: 2026-10-10
branch: "review-app-retriever-chunking"
---

# A local review app that compares retriever/chunking candidate refs with the same input and records the decisions.

What. CLI runner `python -m rfp_assistant.cli stage-review run retriever|chunking --base <ref> --cand <ref> [--cand ...]` - Checks out each ref (worktree is `.`) into its own git worktree and executes it in a new process with secret keys removed. - Input is fixed once from the serving activation. Retriever uses development questions and needles, chunking uses parsed documents. …

Why. (the PR body has no reason section; the grounds for this decision were not recorded)

Source. PR #36 · `review-app-retriever-chunking`
