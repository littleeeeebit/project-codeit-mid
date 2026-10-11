`manifest` → `import_manifest` reads `data_list.csv`, hashes every original and records documents and sources, keeping duplicates and conflicts. `ingest` → `ingest_source` per unique source hash, with these steps:
- Verify the original's bytes.
- Skip a registered recovery artifact.
- Reuse the result when `input_key` (original, parser revision, print, OCR cache, converter) is unchanged.
- Otherwise parse HWP with pyhwp's `hwp5proc` XML walker plus OCR merge, falling back to `HwpHwpxLoader` (`pyhwp_failed`), or parse the PDF plus OCR merge.
- A parse failure quarantines the source with a reason code.
- Success binds an extraction revision to the actual output (`assign_revision`) and writes `elements.jsonl` and `elements` rows. A new revision resets `review_status` to `unreviewed`.

One crashing file is recorded as `error` and the run continues; the CLI exits 1.