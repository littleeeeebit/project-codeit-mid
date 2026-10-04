---
scope: project
severity: contract
triggers: ["화면", "vision", "프레임", "스크린", "캡처", "공유"]
domain: vision
title: "Added Luna/Jev judgment model comparison to the verification screen"
pr: 14
merged: 2026-10-04
branch: "judge-comparison-luna-vs-jev"
---

# Added Luna/Jev judgment model comparison to the verification screen

What. Added a judgment model comparison area to the verification screen. Based on 750 blind reviews of development response execution `A-9ef59b566d64`, it compares Luna judgment (gpt-6-luna) and Jev judgment (TypeSafe `jev-1.13.0`), and determines whether Jev can replace Luna based on predetermined rules.

Why. Reference set: After verifying the receipt hash from the archived pilot runtime, it was copied as read-only. Then, it was divided into 373 for calibration and 377 for evaluation (seed fixed, stratified by item type and judgment). - Luna judgment: Instructions are in English, and Korean questions, answers, and evidence are passed as JSON data. It goes through existing gateway, ledger, and budget approval, and the purpose is `judge_eval`. It can only start after estimation in `plan-run`. - Korean → English bridge: Numbers, amounts, dates, codes, and organization names are replaced with placeholders and translated using a glossary. Items where protected values have changed or Korean remains are treated as untranslatable, and Jev withholds judgment. - Jev: Called as a small HTTPS client within the repository. The threshold was adjusted in the calibration part. …

Source. PR #14 · `judge-comparison-luna-vs-jev`
