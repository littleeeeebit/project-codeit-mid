The route families and the service functions they call:
- account: /api/info, /api/budget, /api/budget/limit, /api/settings/api-key, /api/settings/model.
- ask: /api/documents -> `find_documents`; /api/ask -> `ask`; /api/requests, /{id} -> `request_status` + `may_attach`; /{id}/stream -> `answer_progress`.
- request: cancel, abandon, export, evidence and originals.
- verify: traces, evaluation, judges, maintenance, experiments, fidelity, ingestion, corrections and history.
- gold and drafting: /api/gold/*, /api/drafting/*.
The dependencies are `Res` (`app.state.res`) and `Member` (`service.visitor(member)`, a Principal with every capability). Handlers are sync `def`s that run in Starlette's threadpool.