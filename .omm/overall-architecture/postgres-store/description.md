PostgreSQL 18.6 + pgvector 0.8.6 only, with no fallback. `postgres.lifecycle` gives one bounded psycopg pool per Target (max 8 connections, version-checked on probe). `require_imported_database` refuses startup without the validated `migration_import` / `migration_validation` marker. `init_schema` migrates under an advisory xact lock. Main tables:
- `sources`: hash, format, parse/review status, active_extraction_id.
- `documents`: CSV rows per doc_id.
- `extractions`, `extraction_inputs`, elements; `fidelity_checks`.
- `requests`: request_json snapshot, scope_json, status, cancel_requested, trace_json, result_json.
- `attempts`, `external_attempts`, `adjustments`, `budget_settings`, `database_control.paid_admission`.
- `app_settings`: active_run, active_index; `activations` (history).
- `audit_events`, vector cache and sets (retrieval/vector_store.py).
Files under RFP_DATA_DIR hold extraction artifacts, index payloads, run folders (.runtime/runs, review/, compare/), maintenance state and reports. Backups go to a `-backups` sibling or RFP_BACKUP_DIR.