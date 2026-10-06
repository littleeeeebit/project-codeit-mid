`src/rfp_assistant/api.py` is a FastAPI wrapper:
- `_routes` covers ask, requests, evidence, originals, budget and settings.
- `_verify_routes` covers `/api/verify/*` and gold second review.
- `_dataset_routes` covers drafting and the gold queue.

The `_member` dependency turns `X-Member` into `service.visitor(name)`, a principal that holds every capability. The `_KeySession` ASGI middleware calls `service.bind_request(res, cookie)` around each request, so paid work started by the request uses that browser's key and model. `/api/requests/{id}/stream` is a server-sent-events generator. Every 0.15 s it calls `service.answer_progress`, emits the partial answer whenever it changes, and ends with `event: done`. The pydantic `_Read` models only mirror what the service returns.