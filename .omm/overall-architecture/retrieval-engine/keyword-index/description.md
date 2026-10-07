`KeywordIndex.load(settings, version)` performs these steps:
- reads the `indexes` row and verifies the manifest hash, plus each file under `.runtime/indexes/<version>/` (chunks.jsonl, tokens.jsonl, requirements.jsonl, scope-terms.json);
- builds BM25Okapi over the persisted Kiwi tokens and maps rows per extraction;
- loads every extraction's elements from its artifact, and maps source_hash to the served extraction.
`build_keyword_index` writes a new immutable version atomically and records its chunk and requirement rows. The version is a hash of the chunker, profile, analyzer, review scope, extractions and metadata terms. It moves `active_index` only when no activation exists.