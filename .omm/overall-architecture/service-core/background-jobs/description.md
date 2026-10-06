Paid and long-running verifier work runs in daemon threads started through `_start_job` or directly:
- answer evaluation (`_EVAL_JOBS`)
- judge comparison (`_JUDGE_JOBS`)
- gold drafting (`_DRAFT_JOBS`, under `_DRAFT_LOCK`)
- maintenance (`_MAINTENANCE_JOB`)

Each thread runs in a copied context and is appended to `res._jobs` under `_runner_lock`, so `close()` joins it. Each job receives a `closing` callback or a guard returning `interrupted` so it stops before the next paid call. `_refuse_closed_or_busy` allows one evaluation or judge run at a time. Run state is published to files under `.runtime` before the start call returns, and failures are written as `error.txt`, `interrupted.txt` or `last-error.txt`.