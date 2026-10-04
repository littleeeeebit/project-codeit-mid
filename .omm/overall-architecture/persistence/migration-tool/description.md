`python -m rfp_assistant.migration` has four subcommands:
- `plan --source --output` takes a WAL-consistent SQLite backup snapshot. It requires schema version equality, `integrity_check`/`foreign_key_check`, no views or triggers, and supported column types. It records per-table canonical digests, dependency order, artifact references and SQL callers.
- `import --plan` requires an empty target or a resumable import of the same snapshot and plan, held under an import advisory lock. It copies rows in rowid order with per-table checkpoints, checks source/target parity digests, and leaves `paid_admission=false`.
- `validate --plan` re-digests every table and re-hashes the artifacts, then records `migration_validation`. A failure disables paid admission.
- `embedding-preflight` estimates a dimension's embedding cost against cached payloads without calling the provider.

Errors print the exception class instead of the message for driver errors, so the DSN never leaks.