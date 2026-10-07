Under `.runtime` (paths observed in code):
- `extracted/<source_hash>/<fingerprint>/elements.jsonl`;
- `indexes/<version>/`: the manifest and its files;
- `runs/<run_id>/`: config, traces, scores, report;
- `datasets/dev.jsonl` and `sealed/`;
- `compare/`: tables and caches;
- drafting run directories (`_drafts_dir`), `maintenance/`, `reviews/` prints and page PNGs, and the `ocr/` cache with the Gemini ledger.
Files are written with `write_text_atomic`/`write_jsonl_atomic`, which write a temp file and rename it, retrying the rename for about 3 s on Windows.