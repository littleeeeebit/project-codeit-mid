---
scope: project
severity: preference
triggers: []
domain: ''
title: "Review app: Accepts my notes grouped by named failure types and displays the frequency compared to reviewed answers by candidate and in release reports."
pr: 41
merged: 2026-10-11
branch: "failure-taxonomy-from-notes"
---

# Review app: Accepts my notes grouped by named failure types and displays the frequency compared to reviewed answers by candidate and in release reports.

What. Converts free notes attached to answers into human-accepted failure types and shows "answers with that type of note / answers reviewed by a human" for each type.

Why. Grouping is done via files exported by the coding agent, while the app and CLI only handle validation and display. It adheres to the rules that the review app does not run pipelines or incur costs, and it respects the shared budget (gpt-6-luna clustering calls are not selected). I prevented the AI from creating types on its own. The cost of inspecting the AI's preferences is greater than the cost of writing my own criteria directly, and in judgment model comparisons, the kappa for failure judgment was less than 0.25 for both models. The number of types is placed alongside, not within, the predetermined release criteria. Human error analysis is the evidence for choosing what to fix next, and the passing line must not be moved post-hoc.

Source. PR #41 · `failure-taxonomy-from-notes`
