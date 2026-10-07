---
scope: project
severity: preference
triggers: []
domain: ''
title: "Strict answer grading: pass only if the row's expected state is met, support only if the entire claim has evidence, share conflicting answer rules"
pr: 28
merged: 2026-10-07
branch: "strict-answer-grading"
---

# Strict answer grading: pass only if the row's expected state is met, support only if the entire claim has evidence, share conflicting answer rules

What. State determination: The answer state is `status_ok` only when it matches the `expected_status` of the gold row. To allow other states, `accepted_statuses` must be specified in the row, and `GoldChecker` checks if that list is an allowed state (`EXPECTED_STATUS`) for answer possibility (`answerability`). …

Why. Conflicts with only one alternative are rejected only when in the `conflicting_evidence` state. Since the example in the implementation contract document shows a single-alternative conflict in the `answered` state, that shape is left as is. - Metadata (CSV) rows do not undergo generated answer verification, so conflict rules are not applied. - If all links are `unsupported`, the claim is also left as unsupported (False). This is because it means no citation speaks to any part of the claim. - Debt ratchet: `evaluation.py` 2624→2627, `answers.py` 1068→1070 (reason recorded). - Sealed results and frozen executions were not regraded or modified. …

Source. PR #28 · `strict-answer-grading`
