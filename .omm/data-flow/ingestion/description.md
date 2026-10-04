`cli ingest` → `ingestion.ingest` → `ingest_source` per unique source:
1. Verify the original's bytes.
2. Reuse the previous result when `input_key` is unchanged (original, parser revision, native print, OCR cache, converter).
3. Parse. HWP runs through `hwp5proc` (`run_hwp_converter`, `walk_hwp`). If pyhwp fails and a Hancom print exists, the print's text layer is used, labelled `hwp_print` with a `recovered_from_native_print` warning. PDF runs through PyMuPDF.
4. Merge OCR region text via `ocr.merge`.
5. Bind the extraction revision to the actual output (`assign_revision`). Identical output keeps its revision and review; different output gets a new content ID beside the old artifact.

A new revision resets `review_status` to `unreviewed`. A parse failure sets `parse_status='quarantined'`, `review_status='needs_recovery'` with a `reason_code`. One crashing file is recorded as `error` and the run continues; the CLI exits 1. Per-run diagnostics go to `.runtime/reports/ingest-*.json`. `recover-source` registers an approved PDF conversion for a quarantined original as a new unreviewed revision.