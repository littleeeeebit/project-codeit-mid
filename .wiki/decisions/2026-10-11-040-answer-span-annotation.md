---
scope: project
severity: preference
triggers: []
domain: ''
title: "Review App: Select any part of the answer to leave a memo, and mark each answer as reviewed to provide a denominator for the failure count."
pr: 40
merged: 2026-10-11
branch: "answer-span-annotation"
---

# Review App: Select any part of the answer to leave a memo, and mark each answer as reviewed to provide a denominator for the failure count.

What. On the answer generation comparison screen, selecting text in the summary, claim, next action, conflict value, or missing item lines opens a memo box where you can write what is wrong in your own words. Memos remain in an additional dedicated storage outside the execution folder and git, so they remain highlighted even after reopening, refreshing, exporting, or importing. You can mark each answer as "Reviewed · No issues," and answers with memos are also counted as reviewed. The screen displays "Reviewed answers n / m," which provides a denominator for the failure count. …

Why. (the PR body has no reason section; the grounds for this decision were not recorded)

Source. PR #40 · `answer-span-annotation`
