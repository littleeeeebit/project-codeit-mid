This is the only database: PostgreSQL 18.6 with pgvector 0.8.6, reached through `RFP_DATABASE_DSN` and a bounded `psycopg_pool`. `lifecycle()` probes the server and pgvector versions before creating the pool, and `open_db` refuses when no lifecycle owner exists. Tables observed in use:
- documents and sources: `documents`, `sources`, `extractions`, `elements`, `extraction_inputs`, `fidelity_checks`
- index: `indexes`, `chunks`, `requirements`, `embedding_payloads`, `embedding_sets`, `embedding_set_rows`
- requests and ledger: `requests`, `attempts`, `adjustments`, `budget_settings`, `external_attempts`
- settings and review: `app_settings` (`active_run`, `active_index`), `activations`, `gold_candidates`
- control: `database_control`, `migration_import`, `migration_validation`, `schema_migrations`, `application_mutex`

`tx(immediate=True)` takes `SELECT … FROM application_mutex FOR UPDATE`.