---
scope: project
severity: preference
triggers: []
domain: ''
title: "Organize BidMate exclusively for PostgreSQL and migrate Phase 4 pilot to bidmate_pilot_archive"
pr: 16
merged: 2026-10-05
branch: "erase-sqlite-entirely"
---

# Organize BidMate exclusively for PostgreSQL and migrate Phase 4 pilot to bidmate_pilot_archive

What. Moved the Phase 4 pilot runtime (schema 6) into its own PostgreSQL database, `bidmate_pilot_archive`, with paid admission off and `bidmate_app` untouched. All 27 tables (675,140 rows) match the original in row counts and normalized digests. The 750 reviewed blind items of run `A-9ef59b566d64`, which existed only in the run files, were loaded into a new `pilot_review_items` table. The 17 local SQLite files (6.63 GB) were then deleted, and every tracked document, plan, record and `.omm` graph was rewritten to describe PostgreSQL only, including `docs/operations/release-report.md`, `docs/plan/end-to-end/5-team-operation.md` and `.wiki/gold-drafting.md`.

Why. The user wanted every trace of SQLite gone from the repository, but the pilot data is the only evidence behind the release report and the judge acceptance criteria. So the pilot was copied into PostgreSQL and verified before any file was deleted, and measurements, run IDs, hashes and verdicts were left unchanged. Git history and PR #13's title and branch keep the old name, because no history rewrite was chosen.

Source. PR #16 · `erase-sqlite-entirely`
