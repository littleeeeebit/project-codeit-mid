`.runtime/review/runs/<stage>-<UTC timestamp>-<hex6>/` holds:
- `run.json`: schema `review-run-1`, refs with commits, input and worker hashes, scorer fingerprint, hosts, state.
- `inputs/`: the frozen inputs.
- `worker.py`.
- `candidates/<id>.json` and `.estimate.json` / `.paid.json`.
- `estimate.json`.
- `view.json`, plus `docs/<n>.json` (chunking) and `images/<n>.png` (OCR).

A folder can be exported as a zip and imported on another machine, keeping the host each candidate ran on.