---
scope: project
severity: contract
triggers: ["화면", "vision", "프레임", "스크린", "캡처", "공유"]
domain: vision
title: "Search comparison automation: Axis matrix runner and experiment comparison screen, human selects and switches from the table"
pr: 19
merged: 2026-10-05
branch: "automated-comparison-pipeline"
---

# Search comparison automation: Axis matrix runner and experiment comparison screen, human selects and switches from the table

What. Change the search comparison to a method where a human selects by looking at a table. One runner(`compare --matrix`) runs all variations to create a table, and in the verification > experiment comparison screen, a human selects a row to switch to the service. Golden set approval is handled by an author and a different AI reviewer, and maintenance is run with a single command without a schedule.

Why. Documentation: Planning documents (steps 0, 2, 4, 5) and `docs/rag/` (evaluation, golden-dataset, chunking, retrieval, reranking) have been aligned under one rule. The pipeline runs all variations, an AI reviewer approves the development/sealed golden rows, and a human selects and activates them from the comparison table. The sentence "Do not create reservation automation" in step 5 has been replaced with this rule. - AI reviewer approval: Gold approval, sealed review, and decision file fields receive an automated reviewer whose identity is different from the author. Self-approval by the author continues to be rejected. …

Source. PR #19 · `automated-comparison-pipeline`
