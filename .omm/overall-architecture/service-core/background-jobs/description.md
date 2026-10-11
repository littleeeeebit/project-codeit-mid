Daemon threads started from the 검증 and 데이터셋 screens on the serving process's own gateway:
- `start_drafting` runs gpt-6-luna drafting through `drafting._generate`, within the consented maximum, charged to `gold_eval`.
- `start_answer_evaluation` and `start_judges` run evaluation work.
- `start_maintenance` runs `storage.maintenance.run`.

Each start checks the update fence and whether a job of its kind is already alive, then appends the thread to `res._jobs`. Results go to files under `.runtime` (drafts, maintenance state). An exception becomes `error.txt`; during shutdown it becomes `interrupted.txt`.