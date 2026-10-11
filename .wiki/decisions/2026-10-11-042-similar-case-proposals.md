---
scope: project
severity: preference
triggers: []
domain: ''
title: "Review app: The agent suggests similar cases of accepted failure types, and a human approves them one by one; answers previously marked as 'no issue' before the type existed are marked for re-review."
pr: 42
merged: 2026-10-11
branch: "similar-case-proposals"
---

# Review app: The agent suggests similar cases of accepted failure types, and a human approves them one by one; answers previously marked as 'no issue' before the type existed are marked for re-review.

What. The AI expands the failure types defined by a human to all stored answers, and the human approves or rejects them one by one. Answers marked as 'no issue' before the type existed are marked for re-review.

Why. The division of labor where a human sets the criteria and the AI expands those criteria to multiple answers was actually useful. We did not choose the method where the AI labels all answers on its own. Criteria found later are also applied to answers already read. Therefore, reviewed answers are not considered final and are marked for re-review. The app and CLI do not call models or paid APIs. Searching is done using files exported by the coding agent.

Source. PR #42 · `similar-case-proposals`
