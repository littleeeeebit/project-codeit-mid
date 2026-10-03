# PostgreSQL and pgvector migration handover

## Status and requirements

PostgreSQL persistence replacement is the first priority, as requested on 2026-10-03. The complete live SQLite snapshot has been rehearsed on an isolated, paid-disabled PostgreSQL target. Production cutover and reduced-large embedding acceptance remain incomplete. This handover records implemented commands and prerequisites; the owner approved paid work and a $10 cap on 2026-10-03, while production activation still requires candidate acceptance.

The user chose to expand the development population before dimension selection. Private inputs now contain 53 development-family documents and 413 excerpts (numeric qualifiers, late tables, repeated codes and deadlines). These are inputs for independent label review, not approved gold or a frozen evaluation population. Sealed questions and first sealed results remain untouched.

The assignment starts from spec `migrate-postgresql-pgvector-reduced-embeddings`, revision 2, with subsequent owner updates recorded below. Storage parity, model comparison, shortening acceptance, ANN evaluation, production cutover and rollback are separate requirements. No quality threshold has been weakened. The owner subsequently selected `text-embedding-3-large`, 1,536 dimensions, replacing the 768-first ladder and smallest-passing selection. The selected dimension has not yet passed quality acceptance.

## Observed source and rehearsal

The live runtime is `.runtime/rfp.sqlite3`, schema 6. It has 100 document associations, 98 sources/originals, 178,049 historical chunks, and 18,983 active chunks in keyword index `62bea0c9c27ad3e7`. There was no active retrieval run and no remaining NumPy embedding/cache artifact. Historical tables `members`, `sessions` and `login_failures` are retained even though current initialization no longer creates them. The earlier pilot archive under `.runtime/live-validation/pr8-review-archive/` is a different ledger and was not imported into the live target.

All 27 source tables, 645,947 records and 733 referenced artifacts validated in rehearsal. Counts and canonical typed per-record digests preserve serialized JSON, Unicode, evidence offsets, null/zero distinctions and historical identifiers. The consistent SQLite backup includes committed WAL contents. Snapshot SHA256: `218f2a8c626fb0b42fddb32cb3906bb661aaf5130b88db211b1092793682f67d`. The active keyword manifest SHA256 is `a1ea4e92b53df37a8d95ea999ec41542a5f6c1ea411cf4899378e862a7351885`.

The ledger has 108 settled attempts (98 historical small-model embedding attempts and ten generation attempts), no pending/unknown attempts, $0.163636 settled attempts plus $0.448818 adjustments, totaling $0.612454. Cap and allowance remain $5.00. Historical budget settings and attempt price snapshots were imported unchanged. The imported historical paid flag remains recorded; the independent PostgreSQL `database_control.paid_admission=false` blocks dispatch in rehearsal.

Private evidence is under `.runtime/postgresql-migration/`: `source-plan.json`, its immutable SQLite snapshot, `validation.json`, `embedding-preflight.json`, backup/restore receipts and development inputs. Do not commit these files, connection secrets, originals or gold. Production SQLite was not changed, and no provider calls were made.

## Pinned setup on Windows

Use Python 3.12 in the existing `rfp-assistant` environment, Docker Desktop with Linux containers, and PostgreSQL 18 client tools. The Compose image is pinned by amd64 digest to PostgreSQL 18.6 with pgvector 0.8.6. Python pins are psycopg binary 3.3.6, psycopg-pool 3.3.3 and pgvector-python 0.5.0. The installed Windows dump/restore client is 18.4; its major version matches the server. The unrelated native PostgreSQL installation on port 5432 is untouched.

```powershell
conda activate rfp-assistant
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
docker desktop start
. ./tools/start-postgresql.ps1
$env:RFP_CONFIG_FILE = (Resolve-Path handoff/postgresql-pgvector/config.example.json).Path
```

The script starts `bidmate-postgresql` on `127.0.0.1:55432`, initializes extension 0.8.6, and sets `RFP_DATABASE_DSN` in this shell. It generates a secret in ignored `.runtime/postgresql.env`; never paste that file or print the DSN. The example configuration uses a fake provider and large/1,536. Selecting dimensions does not itself activate a candidate.

The default database backend is PostgreSQL. Initialization and connection failures stop operation; writes never fall back to SQLite. Explicit `database_backend: "sqlite"` remains for historical fixtures and controlled rollback only. Initializing a fresh PostgreSQL database also leaves paid admission disabled. Use a dedicated application database with the public schema.

The real installed extension accepted `vector(16000)` storage and `vector(3072)` values. Standard-vector HNSW and IVFFlat both rejected 3,072 dimensions and accepted 2,000. Storage and ANN limits are different. [pgvector's documented limits](https://github.com/pgvector/pgvector#hnsw) and the [large-model documentation](https://developers.openai.com/api/docs/models/text-embedding-3-large) describe these constraints.

## Plan, bounded import and validation

Create an empty rehearsal database before import. Do not run application `init` on it first: the importer refuses unrelated populated targets. The following database name is the actual rehearsed target; use a new name for another rehearsal.

```powershell
docker exec bidmate-postgresql-database-1 createdb -U bidmate bidmate_import_20261003
docker exec bidmate-postgresql-database-1 psql -U bidmate -d bidmate_import_20261003 -c "CREATE EXTENSION vector VERSION '0.8.6'"
$taskPassword = ([IO.File]::ReadAllText((Join-Path (Get-Location) '.runtime/postgresql.env'))).Trim().Split('=', 2)[1]
$env:RFP_DATABASE_DSN = "postgresql://bidmate:${taskPassword}@127.0.0.1:55432/bidmate_import_20261003"
python -m rfp_assistant.migration plan --source "$PWD/.runtime/rfp.sqlite3" --output "$PWD/.runtime/postgresql-migration/source-plan.json"
python -m rfp_assistant.migration import --plan "$PWD/.runtime/postgresql-migration/source-plan.json" --batch-size 1000
python -m rfp_assistant.migration validate --plan "$PWD/.runtime/postgresql-migration/source-plan.json"
```

`plan` reads the source only and writes a new private consistent snapshot and plan; it makes no target writes or provider calls. Use a new output path if the plan already exists. `import --max-batches 3` bounds a rehearsal interruption; repeat the same import command to resume. The plan and snapshot hashes, source identity, dependency order, per-table checkpoints and final digests are checked. Changed snapshots, unrelated targets and partial row-count divergence are rejected. Repeating a completed import reports `already_complete` and leaves newer target records untouched; use `validate` to diagnose any later divergence instead of overwriting it. Import errors retain successful committed batches and keep admission disabled.

The plan inventories actual tables and AST-detected direct SQL/NumPy callers in application and tools. Validation checks the imported constraints, complete table values, artifact references and hashes. Originals and immutable extraction files remain at their managed paths. The isolated importer does not ingest, parse, generate, reconcile or relabel historical data.

## Pool, transactions and paid ownership

`service.Resources` creates a shared process pool and releases its lease at shutdown; CLI commands hold a lifecycle context and close on exit. The pool is bounded (default eight connections, five-second checkout timeout). Each operation checks out its own connection. Executor threads share the pool, never a transaction. A dedicated gateway session is separate from the operation pool. Provider inference runs after reservation/dispatch transactions and checkouts end. Server statement timeout is 60 seconds and lock timeout five seconds.

PostgreSQL operations that formerly required `BEGIN IMMEDIATE` serialize on the singleton `application_mutex` row. This deliberately preserves allowance checks, request idempotency, reservation and exactly-once settlement semantics while the application remains one worker. Money is BIGINT micro-USD. SQL parameters use registered psycopg adaptation; the audited compatibility boundary translates qmark placeholders and rejects unported PRAGMAs/SQLite conflict shortcuts. Text uses C collation for deterministic identity/tie ordering; original JSON remains TEXT.

The paid owner holds a database-wide session advisory lock, across processes and hosts. Losing the session is terminal for that owner: it cannot reconnect and regain dispatch implicitly. Ownership is checked before admission, at durable dispatch and immediately before real SDK execution. New owners recover conservatively: dispatched outcomes become unknown and retain reserves; unknown billing blocks new dispatch until reconciled. SDK retries remain disabled. Stop/drain application and maintenance writes before backup, cutover or recovery.

## Large-model estimates and vector serving

```powershell
python -m rfp_assistant.migration embedding-preflight --dimensions 1536
python -m rfp_assistant.cli budget-status
```

The observed 768-dimension corpus preflight has 18,548 unique exact payloads, zero genuine large-model cache hits, 7,838,299 input tokens and 79 bounded batches. At $0.13 per million input tokens, its maximum estimate is 1,019,020 micro-USD ($1.019020). Native-reference and comparison-query misses are additional and cannot be finalized until independent development review/freezing. Remaining embedding envelope is 155,697 micro-USD ($0.155697); global remaining cap is 4,387,546 micro-USD. The corpus alone exceeds the envelope. Dimensions do not reduce token-priced embedding charges.

The proposed new rate version is `openai-standard-text-embedding-3-large-2026-10-03`, checked `2026-10-03T09:31:30Z` against [official model pricing](https://developers.openai.com/api/docs/models/text-embedding-3-large). It is in code, but has not been owner-approved or registered into the imported budget. Historical small rates remain intact. The owner subsequently approved paid work and increasing the cap to $10. Record that approval, register the checked large-model rate, and allocate sufficient embedding/reference/query envelopes before dispatch. The initial $5 observations above remain the pre-change audit. `set-envelopes --file <absolute JSON> --actor <owner> --reason <reason>` accepts a map of purpose to integer micro-USD, must sum to the existing cap and cannot lower a purpose below spent/open reservations. It changes neither cap nor history.

After those prerequisites, existing metered commands are:

```powershell
python -m rfp_assistant.cli plan-embeddings --index 62bea0c9c27ad3e7
python -m rfp_assistant.cli build-dense --index 62bea0c9c27ad3e7 --estimate-id <approved-current-estimate>
python -m rfp_assistant.cli freeze-dataset --dataset dev --actor <independent-reviewer> --reason <review-record>
python -m rfp_assistant.cli evaluate-retrieval --dataset dev --runs K0,K1,D,H --index 62bea0c9c27ad3e7 --allow-paid-queries
python -m rfp_assistant.cli compare-runs --run-id <baseline> --run-id <candidate>
python -m rfp_assistant.cli activate-run --run-id <accepted-run> --decision-file <absolute-reviewed-decision.json>
```

These are command syntax references, not instructions to execute paid or activation steps now. Rehearsal admission blocks them. Every batch uses the same ledger/gateway; verified completed payloads are reused, unknown billed batches are never resent. New corpus/query SDK requests explicitly supply large model and dimensions. Small vectors cannot be hits or be resized into large vectors. Activated historical small configurations visibly fall back to keyword retrieval when incompatible with the new process settings.

PostgreSQL serves exact cosine search over the eligible scoped population, with deterministic ties and source/extraction mappings. Homogeneous sets record model, actual dimensions, normalization policy, normalized payload SHA, vector checksum, source index, version, completion and provenance. Every loaded set is verified before use; a partial, corrupt or mixed set is refused. PG selection never silently reads the preserved NumPy store. Kiwi BM25, exact-code lookup, RRF and evidence packing remain unchanged.

`dense.shorten_large_reference` can derive prefixes from verified native 3,072-dimensional large vectors and L2-renormalize while preserving the original and provenance. This is development-only. A paired API-shortening comparison is still required before mixing construction methods in production. The shortening method is described in the [official embeddings guide](https://developers.openai.com/api/docs/guides/embeddings#reducing-embedding-dimensions).

## Backup, isolated restore and rollback boundary

With the application stopped and the PostgreSQL configuration active:

```powershell
python -m rfp_assistant.cli backup --destination D:\rfp-backups\postgresql-20261003 --actor <owner>
docker exec bidmate-postgresql-database-1 createdb -U bidmate bidmate_restore_20261003b
$env:RFP_RESTORE_DATABASE_DSN = "postgresql://bidmate:${taskPassword}@127.0.0.1:55432/bidmate_restore_20261003b"
python -m rfp_assistant.cli restore-check --backup D:\rfp-backups\postgresql-20261003\manifest.json
```

Backup takes the gateway lock and application write mutex, writes a native custom-format full database dump including extension definitions, and verifies canonical tables/ledger plus referenced immutable hashes and copied mutable runtime files. Restore requires a different empty target, holds its gateway lock through restoration, disables paid admission and then validates parity, historical keyword access and conservative recovery. The actual corrected full-database restore passed 19 checks with zero provider calls. An earlier schema-filtered dump omitted the extension; that rehearsal failed, the command was repaired, and the failed isolated target was retained paid-disabled.

Limitations: PostgreSQL restore currently verifies immutable files at their managed original locations and copied mutable files in the backup. It does not relocate an entire runtime into `--staging`; that parameter is currently SQLite-only. A separately restored artifact tree, historical citation opening after relocation, backup identity/active-pointer acceptance and restoration after new PostgreSQL spending remain required. Keep originals and immutable artifact trees alongside the backup and verify them before recovery.

No production writes have moved to PostgreSQL, so the live old SQLite snapshot/configuration are still the rollback baseline. A future cutover must stop/drain both app and maintenance work, preserve unknown attempts, capture and validate a final consistent snapshot, import/verify it, and switch PostgreSQL plus the accepted large set together. A `postgresql-authority.json` marker beside the retired SQLite database makes application opens read-only; no marker has been installed yet. Never enable two ledgers. After new PostgreSQL writes/spending, use a validated consistent PostgreSQL recovery or implement/validate complete mutable-record reconciliation into a rollback target; resuming the stale SQLite ledger is forbidden.

## Validation and outstanding acceptance

Create a separate empty test database (extension only), never use the imported/live database:

```powershell
docker exec bidmate-postgresql-database-1 createdb -U bidmate bidmate_tests
docker exec bidmate-postgresql-database-1 psql -U bidmate -d bidmate_tests -c "CREATE EXTENSION vector VERSION '0.8.6'"
$env:RFP_POSTGRES_TEST_DSN = "postgresql://bidmate:${taskPassword}@127.0.0.1:55432/bidmate_tests"
python -m unittest tests.test_postgres -q
python -m unittest discover -s tests -t . -q
git diff --check
```

Tests create and delete private schemas, reject public application tables, and use fake inference. The full suite passed 288 tests with three skips. Real PostgreSQL checks cover full fixture import parity, interruption/resume/repeat, cap contention, duplicate dispatch/settlement, owner exclusion/loss, restart unknown recovery, actual storage/index dimension limits, pool ownership, scoped original/correction/verifier access, model-cache separation, explicit dimension dispatch, vector validation and synthetic identical-vector NumPy/exact-PG parity. They do not establish private real-provider or retrieval quality acceptance. Without the isolated DSN these integration tests skip; that is not database acceptance.

Still required before completion:

1. Independent review and freezing of the expanded development labels; record the approved $10 budget/rates/envelopes and verify model access.
2. Bounded native-large reference and paired shortening calls, complete genuinely new 1,536-dimensional corpus/query set and acceptance of the owner's selected dimension. The later 1,536 instruction supersedes the 768-first ladder and smallest-passing selection. Retain nDCG@5 (0.02), complete-support (two percentage points), and zero-new-critical-failure thresholds. Report dense/hybrid eligible denominators separately; if 1,536 fails, retain the safe baseline and report incomplete.
3. Actual old-vector storage parity (private old vectors are currently absent), controlled model/shortening comparisons, identical-vector warm/cold p50/p95 benchmarks at one/six clients, storage/build/cost measurements and EXPLAIN ANALYZE. HNSW is not selected; optional filtered ANN/iterative-scan recall acceptance is unimplemented.
4. Dimension-comparison/benchmark automation, sealed/configuration preservation checks, relocated artifact restore and tested rollback after new writes. These do not yet have implemented CLI commands.
5. Final maintenance-window cutover, original evidence and local single/two-document answer acceptance with verifier/review/correction/budget checks. No cutover command or paid-admission enabling procedure is published until these safety prerequisites are implemented and reviewed.
6. Pull request and established independent review. Local execution and owner actions remain completion conditions; mocks cannot replace them.

Continue PostgreSQL review and free preparation first. Do not represent this foundation as completion of the full migration specification.

## Subsequent owner instruction

On 2026-10-03 the owner approved paid migration work, raised the requested operating cap from $5 to $10, and requested an editable limit in Settings for teammates. This updates revision 2's budget-preservation requirement: retain settled spending, purpose reservations and historical records while deliberately changing the approved cap. The full migration/quality/cutover requirements remain. `PUT /api/budget/limit` and the Settings page implement an exact integer micro-USD shared limit with a required reason and audit record. It does not create per-member API accounts or authentication. `register-embedding-rate --actor <owner> --reason <approval>` adds the verified large rate while preserving other model rates and all attempt snapshots.

The approved live limit has now been applied: allowance/cap $10, purpose envelopes embedding $2.50, gold/evaluation $2.50 and interactive $5.00. Historical attempts were compared before/after and remained byte-value identical; settled usage is still $0.612454 and no provider calls were made. The verified large-model rate was registered with owner approval. Private receipt: `.runtime/postgresql-migration/budget-approval.json`. A fresh rehearsal snapshot/import carries these deliberate mutable changes; the original $5 rehearsal is retained for comparison.

The approved rehearsal is database `bidmate_import_approved_20261003`, snapshot SHA256 `73ececde5a6d13ae08323e9dcf4f5be9ee2a42dbd1308a773bbd3869503ae703`. All 27 tables / 645,950 records and 733 artifacts validate. The three extra records are append-only approval audit events; no application history was removed. Paid admission remains false. Updated corpus preflight fits the embedding envelope: remaining $2.343197, estimated $1.019020; total remaining cap $9.387546. Private evidence: `approved-source-plan.json`, `approved-validation.json`, `approved-embedding-preflight.json`.

The Settings save flow passed in actual Chrome/Playwright against this PostgreSQL rehearsal with no inference calls and admission still disabled. It deliberately appended a new settings audit record after snapshot validation; a repeated completed import must preserve this newer target state. The live SQLite ledger was not touched by that browser check. Frontend production build and lint passed. The targeted API/PostgreSQL suite passed 23 tests, with subsequent price-history, ownership/pool and initialization-failure checks. A verification-runner reconciliation test failed once under concurrent work, then all three underlying tests and the standalone `budget-recovery` flow passed without a code change; retain this failure in review evidence rather than calling the first result green.

The owner's subsequent instruction fixes new serving embeddings at 1,536 dimensions. Code defaults, nonsecret configuration and preflight defaults now agree. Historical activation overrides at a different model or dimension use visible lexical fallback until compatible acceptance; they are not relabeled. Existing 768 preflight receipts remain historical, and the 1,536 estimate must be recorded separately. Token costs do not decrease with dimensions.

The separate 1,536 preflight is recorded in `.runtime/postgresql-migration/approved-1536-preflight.json`: 18,548 unique payloads, 7,838,299 tokens, 79 batches, zero genuine large cache hits, and maximum corpus cost $1.019019, fitting the approved $2.343197 remaining embedding envelope. Explicit 1,536 query dispatch/normalization/cache behavior and historical model/dimension override rejection passed two real-PostgreSQL tests in 10.509 seconds. These tests use fake inference; actual provider access remains untested. Paid calls were deliberately deferred during implementation/rehearsal with PostgreSQL admission disabled and development review/freezing pending; no API refusal occurred and owner approval/funding are recorded. The manually requested extra review was stopped by the owner and must not be restarted without a new instruction.
