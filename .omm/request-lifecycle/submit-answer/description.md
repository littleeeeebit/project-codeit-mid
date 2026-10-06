`service.ask` creates `generation_id = uuid4()`, which doubles as the idempotency key. It builds an `AnswerRequest` with `as_of = today`, calls `submit_answer`, and returns `target_key(scope, question, mode, as_of, previous)`. `submit_answer` runs these steps:
1. `_validate_request`: mode, 1–N characters for paid modes, one or two distinct documents (none for corpus, exactly two for compare), identifier lengths and the frozen verifier run.
2. `_conversation`, which refuses a previous turn that belongs to another member, is unfinished or has a different scope.
3. Free modes are created as `running` and executed inline.
4. Paid modes go through `_create(..., 'queued', before_insert=admit)`, which inserts the input snapshot, input hash and config hash under `(member_id, idempotency_key)` in one transaction. A full executor is rejected before any paid work.
5. `runner.submit`. A failure there frees the slot and marks the request `failed` with no paid call.