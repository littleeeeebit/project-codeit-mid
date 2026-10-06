---
scope: project
severity: preference
triggers: []
domain: ''
title: "Judgment golden set and maintenance execution once: Comparing Luna and Jev with variant incorrect answers, from backup to regression table with a single verification button"
pr: 20
merged: 2026-10-05
branch: "judge-set-and-maintenance"
---

# Judgment golden set and maintenance execution once: Comparing Luna and Jev with variant incorrect answers, from backup to regression table with a single verification button

What. `judge-set` The command takes 286 approved correct answers from the evaluation criteria and creates 633 incorrect answers by changing them in seven ways: flipping amounts, units, VAT/conditional words, shifting dates/periods, inserting negation, removing conditions, and citing different evidence. - The decisive test (`judge_set.differs`) discards variants that are not actually different from the original. Each row stores the variant type, changed value, original ID, and known correct answer label. - The set is `. …

Why. | Variant | Item | Luna Pass Rate | Jev (English ladder) Pass Rate | | --- | --- | --- | --- | | Citing different evidence | 250 | 8.4% | 7.0% | | Inserting negation | 206 | 2.4% | 28.3% | | Flipping VAT/conditional words | 85 | 27.1% | 55.0% | | Removing conditions | 28 | 35.7% | 88.5% | | Changing units | 27 | 14.8% | 42.3% | | Changing amounts | 19 | 10.5% | 10.5% | | Shifting dates/periods | 18 | 0.0% | 26.7% | | Total incorrect answers | 633 | 10.3% | 26.8% | | Unmodified correct answers (higher is better) | 286 | 88.8% | 88. …

Source. PR #20 · `judge-set-and-maintenance`
