`Tracing.from_settings` exists only when all three LANGFUSE_* variables resolve. `tracing.run` opens one trace per executed request (trace ID derived from request_id; user = member; environment server/local; release `<branch>@<commit>`). `tracing.step` adds the spans:
- retrieve-evidence
- assemble-evidence
- rewrite-question and generate-answer (generations with usage and ledger cost)
- validate-answer (guardrail)
There are three free scores: citation_valid, insufficient_evidence and evidence_tokens. A secret-shaped value mask runs on export. Per README and the `_guard` helper, tracing failures never change answers or the ledger.