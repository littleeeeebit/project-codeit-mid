`sources.parse_status` and `sources.review_status`.
- `ingest_source` sets `parsed` with a new or kept `active_extraction_id`. A parse failure sets `quarantined`, with `review_status='needs_recovery'` and a `reason_code`.
- A new extraction revision resets review to `unreviewed`. Identical output keeps the extraction and its review.
- The fidelity check sets `auto_verified` or `auto_flagged`. A person's `confirm_fidelity` or `import-reviews` sets `sample_checked` or `reviewed`, and an automatic status never overwrites a human one (README).
- `recover-source` registers an approved PDF conversion as a new, unreviewed revision.

`build_keyword_index` admits `auto_verified`, `sample_checked` and `reviewed` sources, plus `unreviewed` and `auto_flagged` ones when built with `--include-unreviewed`. The operating index does include them, and labels them in traces and on the consultant screen.