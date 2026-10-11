Ask routes:
- `GET /api/documents` → `service.find_documents`, a free metadata and keyword search where askable documents sort first.
- `POST /api/ask` → `service.ask`. It mints a `generation_id` that is also the idempotency key, calls `submit_answer` with `as_of` = today, and returns `{request_id, generation_id, target}`; `target_key` hashes the scope, question, mode, date and previous turn.
- `GET /api/requests` and `GET /api/requests/{id}` → `request_status`, plus `may_attach(owned, target, view)`.
- `GET /api/requests/{id}/stream` is a synchronous SSE generator. It calls `service.answer_progress` every 0.15 s and emits a `data:` frame only when the partial changes, then `event: done`.