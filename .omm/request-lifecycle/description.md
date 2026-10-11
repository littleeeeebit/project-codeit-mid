One paid Ask question from click to validated answer. Trigger: the Ask screen submits a question for a scope (1–2 documents, or All documents as `corpus` mode). What crosses each boundary:
- Browser → API: JSON `{scope:[{doc_id, source_hash}], question, mode, previous_request_id}`.
- API → screen: the ownership record `{request_id, generation_id, target}`.
- Runner: only the request ID crosses into the worker. The worker re-reads the persisted `request_json` snapshot.
- Gateway: paid calls cross as `attempts` rows (reserve → dispatching → settled/unknown/released).
- Outcome: written once as `requests.result_json` + `trace_json` by `_finish`.
- Streaming: the provisional text lives only in `Resources.partials` and reaches the browser through SSE. The validated outcome is fetched by polling `/api/requests/{id}`.

Cancellation: queued → cancelled immediately with no cost. Running → `cancel_requested=1`, so no further paid stage starts, an already-dispatched call still settles, and the result is never attached to the screen.