---
scope: project
severity: preference
triggers: []
domain: ''
title: "Phase 4 pilot results·live DB schema 6 migration·Luna draft procedure document reflection"
pr: 9
merged: 2026-10-02
branch: "docs/phase4-pilot-results"
---

# Phase 4 pilot results·live DB schema 6 migration·Luna draft procedure document reflection

What. Reflect the Phase 4 document commit, which was only in the local review worktree(`b52df9e`), to main. The code was already integrated into `65ed997`, and this PR only changes the documentation.

Why. `docs/operations/release-report.md`: Rewrote the actual corpus pilot results (development execution, first sealed execution, latency) to match the post-merge state. Added the procedure for in-place migration of the live DB on 2026-10-02 (backup → `init --paid-disabled` → re-backup → `restore-check` pass) and the pilot expenditure adjustment records. - `docs/rag/golden-dataset.md`, `.wiki/gold-drafting.md`: Added the `gold generate` procedure reflecting the reason for rejection and the rule for the draft author to withdraw their own row. - `README.md`: Recorded the 4,000 token output limit used in the pilot and the pilot expenditure adjustment details. …

Source. PR #9 · `docs/phase4-pilot-results`
