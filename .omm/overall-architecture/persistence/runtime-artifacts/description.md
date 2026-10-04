`RFP_DATA_DIR` (default `<repo>/.runtime`) holds:
- `extracted/<source_hash>/<fingerprint>/elements.jsonl` (extraction revisions)
- `indexes/<version>/` (keyword and dense manifests)
- `embedding-cache/`
- `runs/<run_id>/` (retrieval and answer runs)
- `drafts/<run_id>/`
- `datasets/*.jsonl` and `sealed/test.jsonl`
- `reviews/printed/<hash16>.pdf` (Hancom prints) and rendered pages
- `ocr/<hash>/` and `ocr/gemini-ledger.jsonl`
- `reports/` and `releases/`

Originals live in `RFP_SOURCE_DIR/files` and are never modified. Old index and extraction artifacts are kept so issued citations and pinned gold rows still resolve.