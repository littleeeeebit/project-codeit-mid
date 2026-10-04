`open_db` has three branches:
- A `postgres.Target` yields a pooled `postgres.Connection` (the pool exists only inside `database_lifecycle`, and a changed DSN is refused).
- An SQLite file with a sibling `postgresql-authority.json` opens read-only via a `mode=ro` URI (a retired snapshot).
- Otherwise it opens a WAL connection with `busy_timeout` and foreign keys.

`tx(immediate=True)` is `BEGIN IMMEDIATE` on SQLite. On PostgreSQL it is `SELECT id FROM application_mutex WHERE id=1 FOR UPDATE`, one global write mutex that preserves SQLite's serialized admission and idempotency semantics (marked with a `ponytail:` comment naming per-account locks as the upgrade). `init_schema` is idempotent, never resets ledger rows, and widens older SQLite schemas in place (`user_version` < 3 rebuilds `sources`; < 5 adds the request columns).