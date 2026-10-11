---
scope: project
severity: preference
triggers: []
domain: ''
title: "To prevent maintenance from stopping at a single source text: re-parsing as a child process with a time limit, including a cancel button and per-file progress display (10/10 stop cause fixed)"
pr: 44
merged: 2026-10-11
branch: "maintenance-ingest-timeout-cancel-progress"
---

# To prevent maintenance from stopping at a single source text: re-parsing as a child process with a time limit, including a cancel button and per-file progress display (10/10 stop cause fixed)

What. Re-parsing is done in a child process. The `_parse_source` of maintenance collection runs in a child process (`ParseWorker`) launched via spawn, and all DB writes remain in the parent. Since results return via pipe pickling, Korean filenames or messages cannot kill the parent. - Per-file time limit. Parsing that exceeds `parse_timeout_seconds` (default 120 seconds) is killed. …

Why. In codeit, `M-20261010-233122` re-read 59 out of 98 source texts in 94 seconds, after which one thread ran at 100% CPU for over 7 hours. I launched `py-spy dump --locals` in the live service process to check the code that was actually running. ```text Thread "rfp-maintenance-M-20261010-233122" (active+gil) _extract_hyperlink_texts_from_para (hwp_hwpx_parser/hwp5.py:897) i: 14, code: 3 load_hwp_hwpx (rfp_assistant/storage/hwp_loader.py:41) _parse_source (rfp_assistant/corpus/ingestion. …

Source. PR #44 · `maintenance-ingest-timeout-cancel-progress`
