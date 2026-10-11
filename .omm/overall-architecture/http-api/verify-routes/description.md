`/api/verify/*` and gold second review:
- Retrieval traces: `verifier_trace` stores a frozen retrieval-only run in `verifier_runs`. `generate_from_run_id` answers from exactly that frozen evidence under its consented estimate.
- `compare_verifier_runs`.
- Answer evaluation: `plan_answer_evaluation` and `start_answer_evaluation`.
- Judges: `plan_judges`, `start_judges` and their results.
- Maintenance: `maintenance_status` and `start_maintenance`.
- Experiments: `experiments`, `experiment_questions` and `activate_experiment`, which goes through `evaluation.activate_decision`.
- Fidelity: overview, rendered page PNG and `confirm_fidelity`.
- Ingestion overview, corrections and history.

Starts refuse while the update fence is up or another job of the same kind is alive.