---
scope: project
severity: contract
triggers: ["화면", "vision", "프레임", "스크린", "캡처", "공유"]
domain: vision
title: "Making the evaluation screen readable at a glance: Korean labels, candidate selector, judgment next to claims, decision from anywhere"
pr: 39
merged: 2026-10-11
branch: "review-ui-readable"
---

# Making the evaluation screen readable at a glance: Korean labels, candidate selector, judgment next to claims, decision from anywhere

What. Fixed issues found by walking through the review app (`review/`) and the evaluation areas of verification (To-do, Evaluation/Release, Judgment Model Comparison, Search Trace, Trace Comparison, Search Configuration Comparison, Dataset) in Chrome at 1440×900 and 1280×800, one screen height at a time. Internal codes were changed to Korean labels, and font sizes were adjusted by order of importance. The answer column was made readable even with many candidates, and decisions can be reached at once from anywhere. …

Why. ### Review App | # | Screen | Issue | Resolution | |---|---|---|---| | 1 | Retriever/Answer Generation | Question type, verification failure (`claim_without_evidence`, etc.), and fact fields appear as internal codes | Use Korean labels in the body, and show codes and question IDs on hover | | 2 | Chunking | Chunk types appear as codes like `requirement_detail` | Display in Korean (requirement details, etc.), codes on hover | | 3 | Answer Generation | Placing the baseline and 3 candidates together narrows the four columns, compressing the answer text | By default, place the baseline and 1 candidate side-by-side and change via "Candidate to view side-by-side" in the toolbar. If "All 4" is selected, each column …

Source. PR #39 · `review-ui-readable`
