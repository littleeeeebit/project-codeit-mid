In `run.json`:
- Free stages are written complete with `view.json` at once.
- Paid stages start at `needs_approval` (estimate.json, not listed by the app).
- `approve` records approval on the estimate.
- `resume` moves the run to `complete` when every approved candidate completed, else to `stopped`, which keeps the finished work and stays resumable.
- `reprice` gives a needs_approval or stopped run a new estimate that needs its own approval.
- `rescore` re-scores a complete or stopped run without calls.