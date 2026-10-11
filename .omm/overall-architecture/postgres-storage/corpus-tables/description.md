Corpus tables:
- `sources`: one row per unique original hash, with parse and review status, active extraction and quarantine reason.
- `documents`: CSV associations, with byte-identical files sharing a source.
- `extractions` and `elements`: parser output revisions.
- `extraction_inputs`: the reuse key per source.
- `fidelity_checks` and `reviews`.
- `metadata_resolutions`.
- `indexes`, `chunks` and `requirements`.
- `gold_candidates` and `gold_reviews`.
- `embedding_estimates` and `eval_estimates`.