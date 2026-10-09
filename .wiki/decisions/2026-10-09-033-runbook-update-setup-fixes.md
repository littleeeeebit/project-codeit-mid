---
scope: project
severity: preference
triggers: []
domain: ''
title: "runbook: codeit update 1-time setup actual record and tool verification command modification"
pr: 33
merged: 2026-10-09
branch: "runbook-update-setup-fixes"
---

# runbook: codeit update 1-time setup actual record and tool verification command modification

What. The "Live check (2026-10-08)" paragraph in runbook 3.2 was different from reality. As of the check on 2026-10-09, the VM was still on the PR #29 bundle branch, and the 1-time setup had not been executed. I replaced this paragraph with a record of the work actually performed on 2026-10-09. - Since there was only 618MB of disk space available, I first cleared the cache that could be re-downloaded: apt, root pip cache 1.7GB, journal exceeding 50MB. ...

Why. `git diff --check` Passed - Verified tools one by one on the VM and confirmed that node, npm, git, and curl are all present 🤖 Generated with [Claude Code](https://claude.com/claude-code)

Source. PR #33 · `runbook-update-setup-fixes`
