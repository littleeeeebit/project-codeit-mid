---
scope: project
severity: contract
triggers: ["화면", "vision", "프레임", "스크린", "캡처", "공유"]
domain: vision
title: "Migrate Streamlit to Next.js/FastAPI and improve UI for three screens"
pr: 10
merged: 2026-10-03
branch: "LittleBitAI/fix-wiki-agent-worktree"
---

# Migrate Streamlit to Next.js/FastAPI and improve UI for three screens

What. Migrate existing Streamlit screens to a Next.js frontend and FastAPI API, and provide asking, verification, and dataset creation as independent pages. Improve the readability of answers and original evidence, and the verification screen automatically displays the actual saved review and decision history.

Why. Draft generation tasks in progress block the next batch and actual paid request entry, leaving a stopped status. Chat blocks duplicate requests during initial submission and performs lookup, cancellation, and interruption using the name at the time of submission. Screen navigation and question modification release ownership of previous requests and clean up submission responses that arrive late after screen navigation. New checkouts also generate Next.js types required for TypeScript checks. - Existing 14 commits from `frontend-redesign-shell` were brought into the current Orca worktree to preserve history. Restoration of posting permissions and handover is `7a084ef`, corrections for review comments are `16daf13`, and fixes for outdated screen selectors in existing browser tests are `d2afd1f`. - GPT-6 in the same worktree. …

Source. PR #10 · `LittleBitAI/fix-wiki-agent-worktree`
