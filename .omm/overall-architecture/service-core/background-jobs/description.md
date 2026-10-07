Verify and dataset routes start daemon threads. Each runs in a copied context and is appended to `Resources._jobs` under `_runner_lock`, so `close()` joins it:
- `start_answer_evaluation`: `answers.run_answers` on a `PinnedResources` that serves the finalist's recorded configuration and charges `gold_eval`.
- `start_judges`: `judges.run`.
- `start_drafting`: `drafting._generate` under the consented maximum, charged to `gold_eval`.
- `start_maintenance`: `maintenance.run`.
`_EVAL_LOCK` allows one evaluation or judge run at a time; drafting and maintenance also allow one each. Progress lives in files under `.runtime` (run directories, last-error.txt, interrupted.txt). A run cut off by a stop shows as interrupted or partial, and resumes on rerun.