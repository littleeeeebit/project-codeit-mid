`build_keyword_index(profile, include_unreviewed, activate)` works as follows:
1. Selects parsed sources by review status.
2. Freezes the scope terms (`metadata_term_snapshot`).
3. Chunks each extraction (`chunking.build_profile`: `structural`, or the fixed baselines).
4. Tokenizes the payloads with Kiwi.
5. Derives the version from the hash of the chunker, profile, analyzer, IDF, review scope, extraction list and metadata-term config.
6. Writes the files to a temporary directory, hashes them into `manifest.json` and renames the directory into place.
7. Inserts the `indexes`, `chunks` and `requirements` rows in one transaction.

An existing ready version is reused. Old index versions are kept for rollback and issued citations.