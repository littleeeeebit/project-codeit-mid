`Resources.__init__` does the following in order:
1. Enters `database_lifecycle`.
2. Calls `postgres.require_imported_database`, `init_schema` and `budget.ensure_budget_row`.
3. With `recover=True`, calls `_own`, which takes the gateway lock and recovers requests and attempts.
4. Chooses the transport: `FakeTransport` for `provider: fake`; an `OpenAITransport` when the process environment or the repository `.env` has `OPENAI_API_KEY`; otherwise `provider_note = NO_API_KEY` until a browser enters a key.

`set_api_key` keeps a session's key only in the transport's memory and records who set it in `key_sources`. `paid_refusal()` explains why this context cannot dispatch. `close()` stops new submissions, shuts down the runner, joins jobs within `shutdown_wait_seconds`, recovers if threads are still alive, closes the transport, flushes tracing, releases the owner and closes the pool. `close()` is also registered with `threading._register_atexit`, so it runs before the executor's join.