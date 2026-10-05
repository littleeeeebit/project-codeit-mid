---
scope: project
severity: preference
triggers: []
domain: ''
title: "Resolved 3 failed responses in PR #13, Phase 5 operational inspection and maintenance, central runner registration, lint cleanup"
pr: 18
merged: 2026-10-05
branch: "close-answer-failures-phase5-runner-lint"
---

# Resolved 3 failed responses in PR #13, Phase 5 operational inspection and maintenance, central runner registration, lint cleanup

What. Included the four steps of specification `close-answer-failures-phase5-runner-lint`(rev 1) in one PR.

Why. Cause (confirmed via execution logs): - `refresh50-ad-migration-design`: The inference tokens were included in the 2,000-token output limit, causing the limit to be exhausted before the response finished. - `refresh50-eg-input-error`, `refresh50-dh-linked-data`: Inference comparing two documents requires evidence from both sides, but verification was allowed to cite only evidence from its own document. - Fix: - Increased the output limit to 4,000 tokens. - Changed the prompt to `grounded-answer-10`. Comparative inference can cite evidence from the other document if it cites at least one piece of evidence from its own document. Factual statements still cite only evidence from their own document. - Found the third cause in the first paid re-run. …

Source. PR #18 · `close-answer-failures-phase5-runner-lint`
