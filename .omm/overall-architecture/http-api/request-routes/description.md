Request routes:
- `POST /api/requests/{id}/cancel` → `cancel_request`.
- `POST /api/requests/{id}/abandon` → `abandon_request`, which cancels only while the request is still queued.
- `GET /api/requests/{id}/export` → `export_request`, with secrets and host paths redacted by `service.redact`.
- `GET /api/requests/{id}/evidence/{evidence_id}` → `open_evidence`: the stored quote, neighbouring elements, location and review warnings.
- `GET /api/originals/{doc_id}/{source_hash}` → `original_download`, which streams the original bytes as an attachment.

Ownership is checked in the service (`member_id` must match).