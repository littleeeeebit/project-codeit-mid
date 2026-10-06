`postgres.lifecycle` probes the server and pgvector versions and opens the bounded pool. `require_imported_database` refuses to continue if any of these hold:
- the recovery fence `bidmate_recovery.control` is open or not verified;
- `migration_import` is not `complete`;
- `migration_validation` did not pass or its snapshot does not match;
- any recorded original, extraction or index file is missing or changed (`references_valid`, which maps foreign host paths through `RFP_PATH_MAP`).

`init_schema` applies the schema under an advisory transaction lock and refuses a newer schema version. A failed validation also closes paid admission.