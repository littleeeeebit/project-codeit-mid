Files under `RFP_DATA_DIR` (default `<repo>/.runtime`):
- `extracted/<hash>/<fingerprint>/elements.jsonl`: the extraction artifacts
- `indexes/<version>/`: manifest and chunk, token, requirement and scope-term files
- `runs/<run_id>/`: frozen retrieval runs
- `compare/`: tables, cells and estimates
- `maintenance/`: `state.json` and reports
- drafting run folders, datasets, sealed sets, review prints, the OCR cache and `archive/`

They are written with `write_text_atomic` and `write_jsonl_atomic`. Index directories are renamed into place from `.tmp-*`. Startup validation hashes every original, extraction and index file the database recorded (`references_valid`).