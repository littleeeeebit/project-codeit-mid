---
scope: project
severity: preference
triggers: []
domain: ''
title: "Fixed 9 instances of excessive emphasis in the wiki lint and reflected wiki decision records"
pr: 6
merged: 2026-10-01
branch: "docs/wiki-lint-emphasis"
---

# Fixed 9 instances of excessive emphasis in the wiki lint and reflected wiki decision records

What. Fixed 9 instances of excessive emphasis found by `repo_lint`. In the 4 documents (`DESIGN.md`, `README.md`, `docs/operations/runbook.md`, `handoff/phase3/README.md`) submitted via PR #5, all bold formatting on list header labels was removed - bold was kept in only two places. …

Why. `python tool/repo_lint.py --repo project-codeit-mid`: No new findings - `git diff --check`: Clean - Document content has not changed. Only the display format has changed 🤖 Generated with [Claude Code](https://claude.com/claude-code)

Source. PR #6 · `docs/wiki-lint-emphasis`
