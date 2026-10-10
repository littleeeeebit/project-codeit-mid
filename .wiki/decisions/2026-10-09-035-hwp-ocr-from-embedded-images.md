---
scope: project
severity: preference
triggers: ["대화\\s*(모델|프롬프트|응답|생성)", "응답\\s*(정책|수리|스키마)", "페르소나", "말투", "gemini", "openai"]
domain: dialogue
title: "Read HWP OCR from embedded images and switch fallback to gpt-5-mini instead of Gemini"
pr: 35
merged: 2026-10-09
branch: "hwp-ocr-from-embedded-images"
---

# Read HWP OCR from embedded images and switch fallback to gpt-5-mini instead of Gemini

What. Read the HWP OCR area from the images inside the HWP file. Hancom printouts are no longer used (only the role of fidelity witness remains). - pyhwp traversal leaves a `picture` marker (BinData reference, page area ratio) where the image control is located. Images inside table cells are placed after the table. - `ocr.merge_hwp` replaces the marker with the `image_text` of the image or deletes it. The marker is not saved, and printout page anchors are not used. …

Why. (the PR body has no reason section; the grounds for this decision were not recorded)

Source. PR #35 · `hwp-ocr-from-embedded-images`
