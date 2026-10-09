---
scope: project
severity: contract
triggers: ["화면", "vision", "프레임", "스크린", "캡처", "공유"]
domain: vision
title: "Verification screen: Group the menu by verifier questions, and present evaluation/release with the conclusion first + compared to the baseline."
pr: 27
merged: 2026-10-07
branch: "verify-page-criteria-and-roles"
---

# Verification screen: Group the menu by verifier questions, and present evaluation/release with the conclusion first + compared to the baseline.

What. Menu (Verification) - Groups: Things to process (To-do) / Pass/Fail status (Evaluation/Release, Judgment model comparison) / Find failure causes (Search tracing, Trace comparison) / Operations · Owner (Search configuration comparison, Maintenance) / Records (Dataset, Collection status, Modification history, Request history). Selection boxes on narrow screens also use the same groups. - Renaming: Execution comparison → Trace comparison, Experiment comparison → Search configuration comparison. To-do remains the default screen. …

Why. Evaluation/Release (Conclusion-first approach like judgment model comparison) 1. Display the release judgment prominently, along with a one-line reason. 2. Show mandatory checks and quality standards in rows. For each row, I have written the Korean name, definition, value, n, baseline, and met/not met/not measured. 3. Show metrics for each candidate of recent development answer evaluations against the same baseline, along with the denominator. For citation precision, I have written it as a pessimistic lower bound until the number of unjudged citations becomes 0, and placed that count next to it. 4. Place cost estimation and paid execution at the bottom. Execution ID, configuration ID, previous execution, and export-review/import-review guidance are collapsed. Backend - `GET /api/verify/overview` returns `evaluation.targets`. …

Source. PR #27 · `verify-page-criteria-and-roles`
