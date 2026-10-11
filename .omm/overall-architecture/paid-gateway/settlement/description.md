In `service._call_and_settle` (and `dense.metered_embed`):
- A pre-execution `ProviderError` → `budget.release(confirmed_pre_execution=True)`.
- Any other failure, or a response with no usage → `mark_unknown`.
- Otherwise `budget.settle`. It runs exactly once and is idempotent on a settled row. It computes the cost from uncached, cached and cache-write tokens at the reserved rates. When the cost exceeds the reservation, it sets `budget_settings.frozen_reason`, which stops all new paid work until inspected. Settling a `reconciled` attempt adds a negative `late-settlement:` adjustment.
- A settlement that fails after a billed call turns the attempt `unknown` and the request `technical_error`.