`.runtime` (`RFP_DATA_DIR`) holds:
- `indexes/<version>/` and the extraction artifacts
- `ocr/<hash>/` region cache
- `reviews/printed/<hash16>.pdf` Hancom prints and `reviews/rendered/`
- `runs/<run_id>/` frozen retrieval runs
- `compare/` (cells, estimates, tables)
- `datasets/dev.jsonl` and `sealed/test.jsonl`
- drafting run folders
- `maintenance/` state and reports
- `review/runs` and `review/decisions`
- `archive/` cold dumps

The originals under `RFP_SOURCE_DIR` are never modified.