---
scope: project
severity: preference
triggers: []
domain: ''
title: "Review App: Review in a flow of notes and failure types like gold with read-only samples where actual consultation responses are pseudonymized and a full map."
pr: 43
merged: 2026-10-11
branch: "traffic-sample-review"
---

# Review App: Review in a flow of notes and failure types like gold with read-only samples where actual consultation responses are pseudonymized and a full map.

What. Enabled team members to read, take notes on, and group into failure types the actual answers they sent to questions in the same way as gold answers. The service DB is read-only, and members are visible only by pseudonym.

Why. Gold questions already contain only expected failures. Unexpected criteria come from actual requests. Samples are selected by rule-based grouping and written reasons instead of embedding clusters. Embeddings do not always represent data, and selections with written reasons can be verified by humans. A team member's request is that person's work. Analysis only requires patterns and not who asked, so pseudonymization and read-only access were used.

Source. PR #43 · `traffic-sample-review`
