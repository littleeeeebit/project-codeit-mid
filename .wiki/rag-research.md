---
scope: project
severity: preference
triggers: ["RAG|rag|리트리버|리랭커|청킹|골든|입찰|토큰|OpenAI|openai"]
domain: rag
reads:
  - 프로젝트 개요.md
  - docs/rag/index.md
  - docs/rag/project-and-data.md
  - docs/rag/budget.md
  - docs/rag/delivery-plan.md
  - docs/plan/end-to-end/0-overview.md
sources:
  - docs/rag/evidence/dataset-audit.json
---

# RFP assistant research before implementation

Read [the research index](../docs/rag/index.md) before choosing the RAG stack. The nine requested topics, primary sources, shared $20 allowance, separate consultant/verification workflows, and two-to-three-day delivery plan are recorded there. These are researched recommendations; no application or model benchmark has been implemented.

The [end-to-end implementation overview](../docs/plan/end-to-end/0-overview.md) links a shared contract specification and five detailed phase assignments, from the first working slice through evaluation/release and remaining-week operation. Each assignment defines files, commands, checks and handoff evidence. It follows the reference project's numbered overview convention while keeping established requirements separate from proposed technical defaults. Planned commands are not implemented or passed checks.

The supplied CSV text is incomplete. Structured parsing was inspected across all 100 originals: 94 HWP and four PDFs produced parseable content; two HWP failed. Plain HWP text conversion lost table contents in the inspected samples. Full ingestion, table fidelity, and explicit failure states precede retrieval tuning.

Use dollar-based progress against the shared allowance, token counts as details, and separate in-flight reservations. Every paid pipeline stage and team experiment needs one shared spending gateway. Outside-key spending cannot be observed in real time without a reconciliation source.

Keep the research body under `docs/rag/` so the repository document catalog can list it. This wiki entry carries the project context and points to those authoritative documents, following the hub wiki's project-page convention.
