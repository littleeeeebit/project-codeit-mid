`step_ingest`:
1. Snapshots (active_extraction_id, parse_status) per source.
2. Runs `ingestion.import_manifest` (CSV rows, sha256, duplicate and conflict groups, sources upsert).
3. Runs `ingestion.ingest(parse=ParseWorker, progress=on_file)`. Before each unique original, `on_file` raises Stopped on cancel or close and writes progress {done, total, current, current_started_at} to state.json.
`ingest_source` reuses unchanged input keys. It returns `timed_out` (no DB write, no input record, so the next run retries) when the worker raises `ParseTimedOut`. Otherwise it records a quarantine or a new extraction revision, resetting review to 'unreviewed' when the revision changes. Any `error` result fails the step. `changed` counts only sources whose active extraction actually moved.