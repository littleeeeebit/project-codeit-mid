---
scope: project
severity: preference
triggers: []
domain: ''
title: "Team shared Langfuse on codeit: migration of previous traces, display of member, environment, and git ref on server/local traces"
pr: 38
merged: 2026-10-10
branch: "shared-langfuse-on-codeit"
---

# Team shared Langfuse on codeit: migration of previous traces, display of member, environment, and git ref on server/local traces

What. We deployed the shared Langfuse (4.50.0, same image digest as the local stack) to team VM `codeit` as Compose project `bidmate-langfuse`. Ports are open only for `127.0.0.1:3100` (web) and `127.0.0.1:9190` (MinIO), and firewall rules and cloud resources have not been changed. Each container has a memory limit (web/worker 1. …

Why. (the PR body has no reason section; the grounds for this decision were not recorded)

Source. PR #38 · `shared-langfuse-on-codeit`
