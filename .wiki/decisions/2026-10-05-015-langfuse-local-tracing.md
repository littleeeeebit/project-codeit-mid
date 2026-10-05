---
scope: project
severity: contract
triggers: ["지연", "latency", "텔레메트리", "턴 조립", "api 조립"]
domain: api
title: "Tracing /api/ask and draft creation execution with local Langfuse"
pr: 15
merged: 2026-10-05
branch: "langfuse-local-tracing"
---

# Tracing /api/ask and draft creation execution with local Langfuse

What. Every `/api/ask` request and every gold-question drafting run is traced to a self-hosted Langfuse v4 stack (`compose.langfuse.yaml`, `tools/start-langfuse.ps1`) bound to its own loopback host 127.0.0.2. An ask trace nests retrieval, evidence assembly, the gpt-6-luna generation (usage and the ledger's settled cost) and answer validation, with three scores from existing checks: `citation_valid`, `insufficient_evidence` and `evidence_tokens`. Secrets are masked at one export gate. `README.md` documents start, stop and the `LANGFUSE_*` settings; `docs/rag/budget.md` notes that Langfuse is observability only.

Why. The user wanted to see, request by request, how well answers are grounded in their evidence. The scores reuse the deterministic citation checks the answer path already runs, so tracing adds no paid calls against the $5 cap. Langfuse only observes: missing keys or a stopped stack change neither answers nor the PostgreSQL ledger, which stays the only budget authority. It runs on 127.0.0.2 because browser cookies ignore ports, and sharing 127.0.0.1 with another local Langfuse broke sign-in for both.

Source. PR #15 · `langfuse-local-tracing`
