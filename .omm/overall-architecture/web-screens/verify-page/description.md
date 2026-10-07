The Verify (검증) screen: a side menu with sections todo, experiments, maintenance, trace, compare, evaluation, judges, dataset, ingestion, corrections and exports. `useVerifyData` loads /api/verify/overview, /fidelity, /ingestion and /traces once. It re-polls the overview every 2 s, but only while an answer run reports `running`. Sections call these routes:
- trace: POST /api/verify/traces, GET /traces/{id}, POST /traces/{id}/generate (the paid answer from a frozen run), GET /api/verify/compare.
- experiments: GET /api/verify/experiments and /{matrix}/{index}/questions, POST /experiments/activate.
- maintenance: GET and POST /maintenance/start.
- evaluation: POST /evaluation/plan and /start.
- judges: /judges/progress, /plan, /start, /results and /disagreements.
- fidelity: confirm, and rendered page PNGs through <img>.
- gold: second review.
- exports: request and trace JSON downloads through <a href>.