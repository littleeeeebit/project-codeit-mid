---
scope: project
severity: preference
triggers: []
domain: ''
title: "Local main matches origin/main, the session notes for 2026-10-05 and the decision records for PR #15 and PR #16 have been committed, and the stale-index lint warning has disappeared because .wiki/corpus.json and .wiki/graph.json are up to date."
pr: 17
merged: 2026-10-05
branch: "sync-main-and-record-pr15-pr16"
---

# Local main matches origin/main, the session notes for 2026-10-05 and the decision records for PR #15 and PR #16 have been committed, and the stale-index lint warning has disappeared because .wiki/corpus.json and .wiki/graph.json are up to date.

What. Local main matches origin/main, the session notes for 2026-10-05 and the decision records for PR #15 and PR #16 have been committed, and the stale-index lint warning has disappeared because .wiki/corpus.json and .wiki/graph.json are up to date.

Why. Hard reset local main to origin/main. — The two local-only commits are already in origin/main (see 2026-10-04 memo) or will be regenerated anyway (corpus.json), as according to the PR #16 session memo, otherwise an index conflict will occur in the next pull. (Discarded: rebasing and removing duplicates (conflicts in files that are regenerated anyway), or leaving main as is (conflicts remain)) If the wiki-agent itself has a decision record generator, use it; if not, notify the person that it cannot be found, and then write the record manually in the PR #14 record format — that person wants the record to be generated in the same way as before if possible, and wants to be explicitly notified if such a tool does not exist …

Source. PR #17 · `sync-main-and-record-pr15-pr16`
