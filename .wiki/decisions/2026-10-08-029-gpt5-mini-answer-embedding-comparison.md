---
scope: project
severity: preference
triggers: []
domain: ''
title: "Default to gpt-5-mini and BGE-m3-ko after the $3 comparison"
pr: 29
merged: 2026-10-08
branch: "gpt5-mini-answer-embedding-comparison"
---

# Default to gpt-5-mini and BGE-m3-ko after the $3 comparison

What. Answering previously selected GPT-6 Luna even though the team key reaches only GPT-5 mini/nano. Answering now defaults to GPT-5 mini, rejects Luna at configuration load and lists only mini/nano in settings. …

Why. The completed comparison answers all 11 requested hybrid embedding rows plus keyword-only K1. Run `E-9b785d7cd167` used the same 35 development questions (five per type, deterministic selection without answer scores), `minimal` reasoning and a 2,000-token combined reasoning/visible-output cap. …

Source. PR #29 · `gpt5-mini-answer-embedding-comparison`
