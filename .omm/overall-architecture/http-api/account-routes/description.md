Account routes:
- `GET /api/info`: build commit, today, maximum question length.
- `GET /api/update/status` and `POST /api/update`: `update.update_status` and `update.request_update`.
- `GET /api/budget` and `PUT /api/budget/limit`: `service.budget_snapshot` and `set_budget_limit`, which change the cap while keeping spending and reservations.
- `GET` and `PUT /api/settings/api-key`, `PUT /api/settings/model`: `service.set_api_key` and `set_generation_model`. These check the key against the model, put it in `Resources.key_sources` under a fresh `secrets.token_urlsafe` session, and write an audit row without the key.