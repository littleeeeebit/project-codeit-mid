`Resources(settings, recover=True)` is created once by `get_resources`:
- It enters the database lifecycle, then calls `require_imported_database`, `init_schema` and `ensure_budget_row`.
- `_own` takes the `GatewayOwner` advisory lock and runs restart recovery: `recover_requests` turns queued and running requests into interrupted; `budget.recover` turns dispatching attempts into unknown and reserved ones into released. Nothing is replayed.
- It builds `FakeTransport` for `provider: fake`, or `OpenAITransport` with the environment key. It also holds `key_sources` and `member_keys` (member keys from 설정, in memory only), `partials` (streamed text per request) and `stage_errors`.
- It caches `KeywordIndex`, `DenseIndex` and the reranker per activated version. `retire_inactive` frees GPU models the activation no longer uses.
- `close()` stops admission, waits up to `shutdown_wait_seconds`, marks leftovers interrupted, closes the transport, flushes tracing and releases the lock.