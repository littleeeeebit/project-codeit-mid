`service.ask` generates the generation_id, which doubles as the idempotency key, and calls `submit_answer`. That function:
1. Validates the request.
2. Checks the conversation chain: same member, terminal turns, same scope.
3. Takes a runner slot under the update fence.
4. Inserts the `requests` row as `queued`, with request_json (the question, scope, mode, as_of, ids and capabilities snapshot), input_hash and the config snapshot (index version, serving run, prompt version, model, rate_version, settings fingerprint).
Nothing is reserved yet. A refused admission returns 400 with 'no paid call' wording.