The 검증 page loads `/api/verify/overview`, `/fidelity`, `/ingestion` and `/traces`. Its tabs map to service functions:
- trace runs (`run_trace`, `generate_from_run_id`)
- experiment tables with activation (`experiments`, `activate_experiment`)
- maintenance (`start_maintenance`, `maintenance_status`)
- answer evaluation and judge comparison (plan, then start, against a consented estimate)
- HWP fidelity pages and confirmation
- gold second review and recent decisions
- the verification history