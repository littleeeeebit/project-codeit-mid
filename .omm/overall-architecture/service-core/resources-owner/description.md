`Resources.__init__` does the following in order:
1. Opens the PostgreSQL pool via `database_lifecycle`.
2. Requires a validated import (`require_imported_database`), runs `init_schema` and `ensure_budget_row`.
3. With `recover=True`, `_own` borrows this process's `GatewayOwner` or takes the advisory lock, then runs `recover_requests` (queued/running → interrupted) and `budget.recover` (dispatching → unknown, reserved → released).
4. Builds `FakeTransport` for provider 'fake', otherwise `OpenAITransport` when OPENAI_API_KEY exists, and attaches `Tracing.from_settings`.
Members' keys entered on 설정 go to `set_api_key`, which creates a new key session in transport memory only; `member_keys` maps member to session. Activation-dependent objects (`index()`, `dense()`, `reranker()`) reload when `active_serving` changes, and `retire_inactive` frees GPU models no longer served. `close()`:
1. Sets `_closed` under `_runner_lock`.
2. Shuts the runner down and joins jobs within `shutdown_wait_seconds`.
3. Recovers again if a job is still alive.
4. Closes the transport and tracing (flush), then releases the lock and pool.