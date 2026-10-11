Paid or long jobs run as daemon threads registered in `res._jobs`, so `close()` joins them:
- answer evaluation (`start_answer_evaluation` → answers.run_answers, gold_eval envelope),
- judge runs (`start_judges` → judges.run, judge_eval),
- drafting (`start_drafting`),
- maintenance (`start_maintenance` → maintenance.run).
The evaluation and judge starts take `_EVAL_LOCK` and `_refuse_closed_or_busy` (one evaluation or judge run at a time). `_start_job` re-checks closed, key and update fence under `_runner_lock` before starting. Maintenance allows one run per process (`_MAINTENANCE_JOB`). A job failure is written to `last-error.txt` in its run directory; rerunning resumes from recorded progress.