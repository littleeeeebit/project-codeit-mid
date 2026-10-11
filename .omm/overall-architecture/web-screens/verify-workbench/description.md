검증 page. It loads /api/verify/overview, /fidelity, /ingestion and /traces, and hosts the tabs:
- trace.tsx: POST /api/verify/traces for a free frozen retrieval run, and /traces/{run_id}/generate for one idempotent paid answer per run.
- evaluation.tsx: plan and start a development answer evaluation.
- judges.tsx: plan, start and view results of the judge comparison.
- experiments.tsx: comparison tables and POST /api/verify/experiments/activate.
- fidelity.tsx: confirm an HWP fidelity verdict.
- gold.tsx: second gold review.
- records.tsx: history and all members' requests.