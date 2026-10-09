---
scope: project
severity: preference
triggers: []
domain: ''
title: "Update the server to the latest GitHub main from a header button"
pr: 31
merged: 2026-10-09
branch: "main-update-banner-button"
---

# Update the server to the latest GitHub main from a header button

What. When GitHub `main` has commits the codeit service is not running, every signed-in member sees an update banner in the shared header. Update → confirm moves the VM to the latest `main` and restarts the service. …

Why. Check (`service/update.py`): the server records its running commit at startup. Every 5 minutes it asks GitHub's public API (no token) for `main`'s head and the commits in between. Only a fast-forward is offered. …

Source. PR #31 · `main-update-banner-button`
