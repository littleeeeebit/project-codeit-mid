# PostgreSQL and pgvector migration handover

## PostgreSQL-only operation (2026-10-04)

BidMate now runs only on PostgreSQL 18.6 + pgvector 0.8.6 (database `bidmate_app`), with `text-embedding-3-large` at a fixed 1,536 dimensions. This was spec `cutover-postgresql-remove-sqlite` rev 1. The application records, billing ledger, keyword and vector retrieval sets all live there. No code path reads or writes SQLite: `database_backend`, the SQLite backup format and rollback path, the NumPy dense-matrix serving path and the importer `rfp_assistant.migration` were deleted after the final import validated and the owner confirmed it. Everything after this section is the pre-cutover history; its SQLite commands, `rfp_assistant.migration` and `config.corpus-before-cutover.example.json` no longer exist.

Operate it as follows:

```powershell
./tools/start-postgresql.ps1                       # container with init: true, so the postmaster is not PID 1
$env:RFP_CONFIG_FILE = (Resolve-Path handoff/postgresql-pgvector/config.example.json).Path
$env:RFP_DATABASE_DSN = "postgresql://bidmate:<password>@127.0.0.1:55432/bidmate_app"   # password from .runtime/postgresql.env
python -m uvicorn rfp_assistant.api:app --host 127.0.0.1 --port 8501 --workers 1
```

Startup refuses, with a message, a missing `RFP_DATABASE_DSN`, an unreachable server and a database without a persisted, passing import validation. There is no fallback. Tests create an isolated database per run from the same server (`RFP_POSTGRES_TEST_DSN`, defaulting to the local server).

The container crashed three times during the cutover with "untracked child process exited with exit code 2". The cause was the postmaster running as PID 1 and reaping orphaned healthcheck processes. `compose.postgresql.yaml` now sets `init: true` and uses an exec-form `pg_isready` healthcheck, and the database was verified intact after each recovery.

### Final import

The final import ran with the UI and all writes stopped. A consistent snapshot of the live SQLite database (SHA256 `ca299be9…`) was imported into `bidmate_app`, and `python -m rfp_assistant.migration validate` passed:

- 27 tables and 674,049 records, matching the source.
- Every referenced artifact hash verified, with 0 inaccessible.
- Historical attempts unchanged.
- Spent total unchanged.
- Zero pending or unknown billing.

The private reports are in `.runtime/postgresql-migration/final/` (`plan-output.json`, `import-output.json`, `validation.json`).

### Vector parity and keyword index

All 18,983 active chunk vectors are in pgvector (1,536 dimensions, set `p8d1aa8594d18b47`). They are byte-identical to the verified ready set, with per-row checksums matching (`final/vector-parity.json`). Keyword index `29f261abafeb1f8c` passes `retrieval.index_compatibility` and is active, so `index_outdated` is cleared.

### HNSW against exact search

`python tools/check_vector_search.py --out <RFP_DATA_DIR>/vector-search` builds an HNSW index on the 1,536 vectors. It then compares the HNSW top 20 with the exact top 20 for every frozen pilot (dev, 55 questions) and whole-corpus (33 needles plus the pilot) passage question, on its own scope and over all documents. Nothing in this check is paid.

Mean recall@20 by `hnsw.ef_search`:

| ef_search | dev scoped | dev unscoped | corpus scoped | corpus unscoped |
| --- | --- | --- | --- | --- |
| 40 | 0.8127 | 0.9355 | 0.7924 | 0.9515 |
| 100 | 0.8536 | 0.9545 | 0.8379 | 0.9682 |
| 200 | 0.9045 | 0.9891 | 0.9152 | 0.9939 |
| 400 | 0.9473 | 0.9982 | 0.9636 | 1.0 |

No value reaches 0.99 in every group. Scoped queries filter the HNSW candidates after the graph search, so the needle passage can drop out. Exact search therefore stays the serving path (`dense_search: "exact"`), and the HNSW index is kept only for measurement.

Warm latency in ms, p50 / p95:

| Mode | Clients | dev scoped | dev unscoped |
| --- | --- | --- | --- |
| Exact | 1 | 3.7 / 6.3 | 183 / 227 |
| Exact | 6 | 8.3 / 18.4 | 238 / 286 |
| HNSW | 1 | 17.3 / 46 | 39.5 / 48.6 |
| HNSW | 6 | 24.9 / 71.4 | 101 / 148 |

EXPLAIN ANALYZE results:

- Exact, scoped: 1.7 ms (top-N heapsort over the scope rows).
- Exact, unscoped: 69.6 ms (sequential scan).
- HNSW, scoped: 34.7 ms.
- HNSW, unscoped: 4.2 ms (index scan on `embedding_payloads_hnsw_1536`).

Exact unscoped search costs roughly 0.2 s per All documents question, which is small next to generation.

### Fusion gate

`python tools/check_large_quality.py --out <RFP_DATA_DIR>/fusion-gate/final-2 --run --max-cost-usd 0.05 --variant …` compares keyword-only K1 with seven fusion settings on two populations:

- The frozen 55-question pilot (dataset `a3b4d5cc…`) on its own scopes.
- The 88-question whole-corpus set: the pilot unscoped plus 33 needles.

The selected setting is `keyword_first:60:1.0:6`. The BM25 top 6 keep their order, then weighted RRF (k 60, dense weight 1.0) fills the remaining slots. Evidence limits are unchanged.

| Population | Setting | nDCG@5 | Complete support | New critical failures vs K1 |
| --- | --- | --- | --- | --- |
| Pilot, scoped | K1 | 0.9405 (n=48 graded) | 53/55 | — |
| Pilot, scoped | keyword_first:60:1.0:6 | 0.9405 | 53/55 | 0 |
| Whole corpus | K1 | 0.7832 (n=88) | 71/88 | — |
| Whole corpus | keyword_first:60:1.0:6 | 0.7832 | 71/88 | 0 |

Plain RRF (`rrf:60:1.0`, `rrf:60:0.25`) still loses `refresh50-b-training-handover` and `refresh50-eg-input-error`: dense ranks push K1's packed rows out of the top 5. `keyword_first` with a head of 3 also fails. The gate's query embeddings were 33 settled attempts costing 466 micro-USD. That setting first served through run `H-0f2bf03e9d`. It was superseded after the owner's acceptance round below.

### Owner acceptance round: routing, overview questions and evidence depth

The owner asked an All documents question, "한영대학교의 사업에 대해 알려줘. 어떤 사업을 하는거야?". The answer said the project scope could not be explained. Two causes were found:

- Routing added a wrong document. 서영대학교 shares only the generic title words 대학교 and 사업 with the question. Its IDF weight was 4.457, which passed the cutoff of half of 한영대학교's 7.824. `route_corpus` now takes documents best first and weighs each only on terms no earlier document explains. Two named projects still both route, and editions with the same name are kept.
- Overview questions matched filler words. Once the documents are known, the restated project name is dropped as non-discriminating, and BM25 then matched words like 알리 and 대하 (a login-security table came first). Now, when a question names its documents and no other content noun remains, evidence is ranked by dense similarity within the named documents. That limitation is recorded as `name_only_question:dense_within_named_documents`. Title vocabulary that names a project (사업, 구축, 시스템) is not counted as content. A question with a fact term ("…의 하자보수 기간은?") keeps the keyword path.

The owner also set 50 candidates per channel and after fusion, asked to measure 10, 15, 20, 25 and 30 evidence units, and asked for the local reranker (`BAAI/bge-reranker-v2-m3`, revision `953dc6f6…`, CUDA fp32) to be measured too. The command was:

```powershell
python tools/check_large_quality.py --out <RFP_DATA_DIR>/fusion-gate/depth50-units --run --max-cost-usd 0.05 --variant keyword_first:60:1.0:6 --depth 50 --units 10,15,20,25,30 --rerank-revision 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e
```

Each setting is gated against K1 at the same depth and evidence limits. The token budget is 400 tokens per unit, plus 800 for the first unit. Among passing settings, the most complete support wins, then nDCG@5, then fewer units. No query embedding was paid; all vectors were cached.

| Setting | Gate | Pilot scoped (nDCG@5, complete) | Whole corpus (nDCG@5, complete) | Failing reason |
| --- | --- | --- | --- | --- |
| Hybrid, 10 units | pass (selected) | 0.9405, 55/55 (K1 54/55) | 0.8195, 73/88 (K1 72/88) | — |
| Hybrid, 15 units | pass | 0.9405, 55/55 (K1 55/55) | 0.8195, 73/88 (K1 72/88) | same support, more tokens |
| Hybrid, 20, 25 and 30 units | fail | 0.9405, 55/55 | 0.8195, 73/88 (K1 75/88) | loses `refresh50-a-stabilization` and `refresh50-ad-migration-design` |
| Hybrid + reranker, 10 and 15 units | fail | 0.8948, 50/55 and 52/55 | 0.7656, 70/88 and 72/88 | three to five new critical failures, including `refresh50-eg-input-error` and `refresh50-dh-linked-data` |
| Hybrid + reranker, 20 and 25 units | fail | 0.8948, 54/55 and 55/55 | 0.7656, 74/88 | new critical `refresh50-dh-linked-data` and `refresh50-a-stabilization` |
| Hybrid + reranker, 30 units | fail | 0.8948, 55/55 | 0.7656, 76/88 (K1 75/88) | nDCG@5 below K1 |

The reranker reorders the whole candidate list, including the BM25 head that `keyword_first` protects, and loses rows K1 keeps. It is not served. More than 10 evidence units help keyword-only retrieval more than hybrid, which fills later slots with dense rows.

`evaluate-retrieval --dataset dev --runs K1,H` recorded the selected limits as run `H-af9967ca81`. It reached packed complete 1.0 (55/55) with 0 critical failures, against K1-0351093f32's 54/55 and 1. The run was activated with a decision file. The top 5 equals `H-0f2bf03e9d`'s on all 55 rows, so the earlier pool review of the same 83 passages carries over. Serving now uses keyword_first:60:1.0:6, 50/50 candidates, 10 evidence units (4,000/4,800 tokens) and exact pgvector search.

### Whole-corpus needle set

The owner's point was that selecting one or two documents first proves nothing. To test retrieval without that help, a needle set was built:

- gpt-6-luna drafted 33 needle questions (10 drafting attempts, $0.0132).
- Each was independently reviewed against the original page before freezing (`needles/corpus-freeze.json`).
- Each targets one passage over 22 sources: 27 on middle pages and 6 on late pages.
- The questions use exact identifiers, numbers and table cells.

Over all 18,983 chunks with the serving setting (run `H-af9967ca81`, including the routing fix):

| Measure | Hits | Rate | Wilson 95% |
| --- | --- | --- | --- |
| Target chunk in top 5 | 32/33 | 0.9697 | [0.8468, 0.9946] |
| Target chunk in top 10 | 32/33 | 0.9697 | [0.8468, 0.9946] |

K1 misses the same needle, needle-24, a table-cell question. Before the routing fix both systems also missed needle-09: 31/33, 0.9394, Wilson [0.8039, 0.9832].

### Paid end-to-end check

Paid admission was enabled on PostgreSQL only. The same 26 questions ran through the app on the serving run `H-af9967ca81` (private results in `.runtime/postgresql-migration/e2e-depth50/`):

| Mode | Requests | Answered |
| --- | --- | --- |
| All documents | 12 | 12 |
| Two-document comparison | 9 | 8 answered, plus `refresh50-ah-ip` returning `conflicting_evidence` as expected |
| Single document | 5 | 5 |

- Every request reached its expected status.
- 24 of 26 cite the expected passages.
- Listed citation failures:
  - `refresh50-ad-migration-design` (comparison): 4 of 13 required evidence groups retrieved and cited.
  - `cutover-late-06` (single): both groups retrieved, neither cited.
- All 26 attempts were reserved, dispatched and settled in the PostgreSQL ledger; the query vectors were cached.
- The budget meter and `budget-status` agree: spent $1.820089, ledger revision 1409, pending 0, unknown 0.

The first run on `H-0f2bf03e9d` (6 evidence units, 20 candidates) answered 21 of 26 and cited correctly in 23. Its failures were `refresh50-ad-migration-design` (output truncated), `refresh50-eg-input-error` and `pair-warranty`. All 28 of its attempts settled.

A headless browser check against `bidmate_app` (private screenshots in `.runtime/browser-check-cutover/`) covered four screens:

- All documents: one question, answered from PSR-002 of the 벤처기업협회 RFP, settled at $0.000683.
- Two-document comparison: QUR-006 against QUR-002, answered with citations from both documents, settled at $0.000939.
- Verification: loads.
- Settings: shows the $10 cap and spent amount.

The check found that the Verification fidelity overview still used a SQLite-only ungrouped `GROUP BY`. It is fixed and covered by `tests.test_api`.

New paid work in this PR totals $0.062712 against the $1.00 ceiling, which `--max-cost-usd` and the envelopes enforce:

| Item | Cost (USD) |
| --- | --- |
| Needle drafting | 0.013226 |
| Evaluation query embeddings | 0.001158 |
| Interactive embeddings | 0.000032 |
| First paid end-to-end answers | 0.019744 |
| Browser check and the owner's first acceptance question | 0.002386 |
| Paid end-to-end rerun on `H-af9967ca81` | 0.026166 |

### Archive and rollback

Cold archives are kept under `.runtime/archive/` (private, ignored, referenced by no code; see `ARCHIVE.json`):

- `sqlite-final-2026-10-04/`: the final `.sqlite3` snapshot (`ca299be9…`) and the former live file (`ceead767…`).
- `postgresql-2026-10-04/`: a `python -m rfp_assistant.cli backup` custom-format dump (`database.dump`, `dc71fe72…`) with its manifest. Its ledger: spent 1,791,537 micro-USD, 438 attempts settled, pending and unknown 0.

Rollback is a restore of that dump. Create an empty database, set `RFP_RESTORE_DATABASE_DSN` to it and run `python -m rfp_assistant.cli restore-check --backup <abs>/postgresql-2026-10-04/manifest.json`. The restore-check passed 39 of 39 checks with paid admission disabled. After a pass, point `RFP_DATABASE_DSN` at the restored database and re-enable paid admission deliberately with `paid on`.

## Status and requirements

PostgreSQL persistence replacement is the first priority, as requested on 2026-10-03. The complete live SQLite snapshot has been rehearsed on an isolated, paid-disabled PostgreSQL target. The approved large/1,536 corpus build has now completed with real provider calls, as recorded below. Production cutover and reduced-large embedding acceptance remain incomplete. This handover records implemented commands and prerequisites; the owner approved paid work and a $10 cap on 2026-10-03, while production activation still requires candidate acceptance.

Initial private development inputs contained 53 development-family documents and 413 excerpts (numeric qualifiers, late tables, repeated codes and deadlines). Those inputs alone were not reviewed labels. On 2026-10-04, the owner designated the implementation agent to review labels independently of their model drafter. Fifty previously source-reviewed pilot rows and five additional original-PDF-reviewed later-page questions were frozen before comparison. The selected serving dimension remains fixed at 1,536. Sealed questions and first sealed results remain untouched.

The assignment starts from spec `migrate-postgresql-pgvector-reduced-embeddings`, revision 2, with subsequent owner updates recorded below. Storage parity, model comparison, shortening acceptance, ANN evaluation, production cutover and rollback are separate requirements. No quality threshold has been weakened. The owner's later explicit instruction, "Use 1,536 dimensions," replaces the 768-first ladder and smallest-passing selection for this task. This changes the candidate choice, not acceptance: freeze the independently reviewed development population before quality evaluation, compare against the bounded native-large reference, and retain every scope/code/critical-support gate before activation. The selected dimension has not yet passed quality acceptance.

The current task specification is revision 3, incorporating the owner-selected dimension, authorized pre-freeze corpus construction and editable $10 cap. The recovery repairs below preserve its acceptance and cutover requirements.

## Observed source and rehearsal

The live runtime is `.runtime/rfp.sqlite3`, schema 6. It has 100 document associations, 98 sources/originals, 178,049 historical chunks, and 18,983 active chunks in keyword index `62bea0c9c27ad3e7`. There was no active retrieval run and no remaining NumPy embedding/cache artifact. Historical tables `members`, `sessions` and `login_failures` are retained even though current initialization no longer creates them. The earlier pilot archive under `.runtime/live-validation/pr8-review-archive/` is a different ledger and was not imported into the live target.

All 27 source tables, 645,947 records and 733 referenced artifacts validated in rehearsal. Counts and canonical typed per-record digests preserve serialized JSON, Unicode, evidence offsets, null/zero distinctions and historical identifiers. The consistent SQLite backup includes committed WAL contents. Snapshot SHA256: `218f2a8c626fb0b42fddb32cb3906bb661aaf5130b88db211b1092793682f67d`. The active keyword manifest SHA256 is `a1ea4e92b53df37a8d95ea999ec41542a5f6c1ea411cf4899378e862a7351885`.

At the initial audit, the ledger had 108 settled attempts (98 historical small-model embedding attempts and ten generation attempts), no pending/unknown attempts, $0.163636 settled attempts plus $0.448818 adjustments, totaling $0.612454. Cap and allowance were $5.00. Historical budget settings and attempt price snapshots were imported unchanged. The imported historical paid flag remains recorded; the independent PostgreSQL `database_control.paid_admission=false` blocks dispatch in rehearsal. Subsequent authorized cap changes and paid construction are recorded below.

Private evidence is under `.runtime/postgresql-migration/`: `source-plan.json`, its immutable SQLite snapshot, `validation.json`, `embedding-preflight.json`, backup/restore receipts and development inputs. Do not commit these files, connection secrets, originals or gold. The initial rehearsal changed no production SQLite records and made no provider calls.

## Pinned setup on Windows

Use Python 3.12 in the existing `rfp-assistant` environment, Docker Desktop with Linux containers, and PostgreSQL 18 client tools. The Compose image is pinned by amd64 digest to PostgreSQL 18.6 with pgvector 0.8.6. Python pins are psycopg binary 3.3.6, psycopg-pool 3.3.3 and pgvector-python 0.5.0. The installed Windows dump/restore client is 18.4; its major version matches the server. The unrelated native PostgreSQL installation on port 5432 is untouched.

```powershell
conda activate rfp-assistant
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
docker desktop start
./tools/start-postgresql.ps1
$env:RFP_CONFIG_FILE = (Resolve-Path handoff/postgresql-pgvector/config.example.json).Path
```

The script starts `bidmate-postgresql` on `127.0.0.1:55432` and initializes extension 0.8.6. It generates a secret in ignored `.runtime/postgresql.env`; never paste that file or print the DSN. It leaves `RFP_DATABASE_DSN` unchanged and does not select an application database. The PostgreSQL example configuration uses a fake provider and large/1,536 and is for isolated rehearsal only. Explicitly select the imported target in the import commands below before running maintenance/application commands; an empty or incomplete import is refused before schema initialization. Selecting dimensions does not itself activate a candidate.

The implementation default database backend is PostgreSQL. The production launch must explicitly use the existing SQLite ledger/configuration until the reviewed maintenance-window cutover (the root README and runbook show that command). Initialization and connection failures stop operation; writes never fall back to SQLite. Explicit `database_backend: "sqlite"` remains for historical fixtures, live operation before cutover, authorized pre-cutover construction and controlled rollback. An explicit PostgreSQL `init` remains a schema-only maintenance action; it leaves paid admission disabled, does not authorize application startup, and must never precede a migration import. Application startup and ordinary maintenance require the complete, canonically verified import marker. Use a dedicated application database with the public schema.

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

The plan inventories actual tables and AST-detected direct SQL/NumPy callers in application and tools. Validation checks the imported constraints, complete table values, artifact references and hashes, including files named by index manifests. It invalidates the previous attestation and disables target paid admission before checking, then records success only when every check passes. Failed or interrupted validation cannot authorize startup. Restoring a missing artifact requires rerunning validation. Table import completion alone is insufficient; older rehearsal targets must validate successfully to acquire the new attestation. Startup also rechecks validated artifact hashes. Originals and immutable extraction files remain at their managed paths. The isolated importer does not ingest, parse, generate, reconcile or relabel historical data.

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
docker exec bidmate-postgresql-database-1 psql -U bidmate -d bidmate_restore_20261003b -Atc "SELECT report_json FROM bidmate_recovery.receipt WHERE id=1"
```

Backup takes the gateway lock and application write mutex, writes a native custom-format full database dump including extension definitions, and verifies canonical tables/ledger plus referenced immutable hashes and copied mutable runtime files. Restore needs only the backup and a distinct empty `RFP_RESTORE_DATABASE_DSN`; the configured primary database can be unreachable or its DSN unset. The CLI dispatches recovery before opening the primary pool or enforcing its import guard, using configuration and a fake provider without opening a source SQLite database. Restore holds both target gateway and import locks through restoration, verification and publication. It commits a separate `bidmate_recovery.control` fence before native `pg_restore --single-transaction --exit-on-error`; that schema is excluded from dumps and restores. Copied admission/import flags cannot override the fence. Failure disables copied admission, invalidates validation and keeps the target fenced; connection loss leaves the committed fence in place. Only successful checks publish readiness on the same lock-owning connection, with paid admission still false. The earlier corrected full-database restore passed 19 checks with zero provider calls. An earlier schema-filtered dump omitted the extension; that rehearsal failed, the command was repaired, and the failed isolated target was retained paid-disabled.

Limitations: PostgreSQL restore currently verifies immutable files at their managed original locations and copied mutable files in the backup. It does not relocate an entire runtime into `--staging`; that parameter is currently SQLite-only. A separately restored artifact tree, historical citation opening after relocation, backup identity/active-pointer acceptance and restoration after new PostgreSQL spending remain required. Keep originals and immutable artifact trees alongside the backup and verify them before recovery.

The authoritative PostgreSQL recovery receipt is now `bidmate_recovery.receipt`, not a JSON file beside the backup. The final transaction publishes its `report_json` with validation and readiness together, after pool shutdown. It forces `synchronous_commit=on` and requires server `fsync=on`. Startup and paid enforcement reject a recovery target without a committed successful receipt. The CLI returns the receipt location; the read-only SQL command above displays the complete durable report. The recovery schema remains excluded from dumps, so a new restore must produce its own receipt. Earlier restored targets without this database receipt are refused; use a fresh isolated restore target, never manually promote an old fence. A valid new restore attempt removes the obsolete latest JSON receipt and never publishes a new filesystem success report.

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

1. Independent review and freezing of the expanded development labels. The approved $10 budget/rates/envelopes and real large-model access are now evidenced below.
2. Bounded native-large reference and paired shortening calls, new evaluation/interactive query vectors and acceptance of the owner's selected dimension. The genuinely new corpus construction is now complete (receipt below). The later 1,536 instruction supersedes the 768-first ladder and smallest-passing selection. Retain nDCG@5 (0.02), complete-support (two percentage points), and zero-new-critical-failure thresholds. Report dense/hybrid eligible denominators separately; if 1,536 fails, retain the safe baseline and report incomplete.
3. Actual old-vector storage parity (private old vectors are currently absent), controlled model/shortening comparisons, identical-vector warm/cold p50/p95 benchmarks at one/six clients, storage/build/cost measurements and EXPLAIN ANALYZE. HNSW is not selected; optional filtered ANN/iterative-scan recall acceptance is unimplemented.
4. Dimension-comparison/benchmark automation, sealed/configuration preservation checks, relocated artifact restore and tested rollback after new writes. These do not yet have implemented CLI commands.
5. Final maintenance-window cutover, original evidence and local single/two-document answer acceptance with verifier/review/correction/budget checks. No cutover command or paid-admission enabling procedure is published until these safety prerequisites are implemented and reviewed.
6. Pull request and established independent review. Local execution and owner actions remain completion conditions; mocks cannot replace them.

Continue PostgreSQL review and free preparation first. Do not represent this foundation as completion of the full migration specification.

## Subsequent owner instruction

On 2026-10-03 the owner approved paid migration work, raised the requested operating cap from $5 to $10, and requested an editable limit in Settings for teammates. This updates revision 2's budget-preservation requirement: retain settled spending, purpose reservations and historical records while deliberately changing the approved cap. The full migration/quality/cutover requirements remain. `PUT /api/budget/limit` and the Settings page implement an exact integer micro-USD shared limit with a required reason and audit record. It does not create per-member API accounts or authentication. `register-embedding-rate --actor <owner> --reason <approval>` adds the verified large rate while preserving other model rates and all attempt snapshots.

The approved live limit was applied before construction: allowance/cap $10, purpose envelopes embedding $2.50, gold/evaluation $2.50 and interactive $5.00. Historical attempts were compared before/after and remained byte-value identical; settled usage at that checkpoint was $0.612454 and no provider calls had been made. The verified large-model rate was registered with owner approval. Private receipt: `.runtime/postgresql-migration/budget-approval.json`. A fresh rehearsal snapshot/import carries these deliberate mutable changes; the original $5 rehearsal is retained for comparison.

The approved rehearsal is database `bidmate_import_approved_20261003`, snapshot SHA256 `73ececde5a6d13ae08323e9dcf4f5be9ee2a42dbd1308a773bbd3869503ae703`. All 27 tables / 645,950 records and 733 artifacts validate. The three extra records are append-only approval audit events; no application history was removed. Paid admission remains false. Updated corpus preflight fits the embedding envelope: remaining $2.343197, estimated $1.019020; total remaining cap $9.387546. Private evidence: `approved-source-plan.json`, `approved-validation.json`, `approved-embedding-preflight.json`.

The Settings save flow passed in actual Chrome/Playwright against this PostgreSQL rehearsal with no inference calls and admission still disabled. It deliberately appended a new settings audit record after snapshot validation; a repeated completed import must preserve this newer target state. The live SQLite ledger was not touched by that browser check. Frontend production build and lint passed. The targeted API/PostgreSQL suite passed 23 tests, with subsequent price-history, ownership/pool and initialization-failure checks. A verification-runner reconciliation test failed once under concurrent work, then all three underlying tests and the standalone `budget-recovery` flow passed without a code change; retain this failure in review evidence rather than calling the first result green.

The owner's subsequent instruction fixes new serving embeddings at 1,536 dimensions. Code defaults, nonsecret configuration and preflight defaults now agree. Historical activation overrides at a different model or dimension use visible lexical fallback until compatible acceptance; they are not relabeled. Existing 768 preflight receipts remain historical, and the 1,536 estimate must be recorded separately. Token costs do not decrease with dimensions.

The separate 1,536 preflight is recorded in `.runtime/postgresql-migration/approved-1536-preflight.json`: 18,548 unique payloads, 7,838,299 tokens, 79 batches, zero genuine large cache hits, and maximum corpus cost $1.019019, fitting the approved $2.343197 remaining embedding envelope. Explicit 1,536 query dispatch/normalization/cache behavior and historical model/dimension override rejection passed two real-PostgreSQL tests in 10.509 seconds. Those tests use fake inference; provider access had not yet been attempted at that checkpoint. Paid calls were initially deferred during implementation/rehearsal with PostgreSQL admission disabled and development review/freezing pending; no API refusal occurred and owner approval/funding were recorded. The manually requested extra review was stopped by the owner and must not be restarted without a new instruction.

## Authorized paid corpus build

The owner then explicitly instructed execution of paid corpus work within the approved scope. This permits building the already selected 1,536-dimensional corpus before development-label review; quality evaluation and serving activation still require independent reviewed/frozen labels and the existing thresholds. PostgreSQL rehearsal admission was not enabled. The sole live SQLite ledger billed the transitional construction through its existing exclusive gateway, preserving a consistent ledger for final import. This is explicit pre-cutover execution, not a fallback after PostgreSQL failure or a second spendable ledger.

Actual execution used `service.Resources(recover=True)` and the same `dense.plan_embeddings` / `dense.build_dense` path as the maintenance CLI, with an ignored progress/receipt wrapper. It took a consistent pre-call backup, verified the current envelope and estimate, explicitly dispatched `text-embedding-3-large` / 1,536 with SDK retries disabled, and compared every historical attempt before/after. No generation calls or activation were made.

| Paid result | Observed value |
| --- | --- |
| Model / dimensions | `text-embedding-3-large` / 1,536 |
| Exact unique payloads / chunk rows | 18,548 / 18,983 |
| Genuine large cache hits before build | 0 |
| Provider calls / settled batches | 79 / 79 |
| Input tokens | 7,838,299 |
| New settled cost | 1,019,019 micro-USD ($1.019019) |
| Total historical + new spending | 1,631,473 micro-USD ($1.631473) |
| Remaining global $10 cap | 8,368,527 micro-USD ($8.368527) |
| Pending / unknown billing | 0 / 0 |
| Construction wrapper duration, including plan/backup | 392.422 seconds |
| Transitional immutable NumPy candidate | `dce96f9d2090fedc` |
| Serving pointer | Original keyword index `62bea0c9c27ad3e7`; no activated run |

The vectors are genuinely new large-model outputs. All rows pass dimension, finite/nonzero, unit normalization, checksum and source mapping checks. Historical small vectors/attempts were not relabeled. NumPy is a preserved construction/comparison artifact, and will not be used implicitly when PostgreSQL is selected. These results establish real provider access and complete corpus construction, not retrieval quality.

Private receipts are `paid-corpus-estimate.json` (estimate `b7211b494d36`), `paid-corpus-receipt.json`, and `before-paid-corpus-backup.json` under `.runtime/postgresql-migration/`. Do not copy source caches or database files into Git. To reproduce/resume this explicitly authorized pre-cutover path, stop the UI and use the existing maintenance commands with the transitional nonsecret configuration; retain the single live data directory and ledger:

```powershell
$env:RFP_CONFIG_FILE = (Resolve-Path handoff/postgresql-pgvector/config.corpus-before-cutover.example.json).Path
python -m rfp_assistant.cli plan-embeddings --index 62bea0c9c27ad3e7
python -m rfp_assistant.cli build-dense --index 62bea0c9c27ad3e7 --estimate-id <fresh-estimate>
```

Unchanged reruns must reuse verified cache entries with zero new provider calls. Unknown billed outcomes still block resending. Do not use this SQLite configuration after the PostgreSQL authority/cutover marker is installed.

The post-paid consistent source snapshot is `paid-source-plan.json`, SHA256 `89bf68b3cc12c650c045fcfd2e4ccf7d0b45e98815b5395de69ab704034ece6c`, with 27 tables / 646,033 records. Target `bidmate_corpus_20261003` remains paid-disabled. The provider-free cache-transfer command verifies source plan/index identity, complete imported ledger, each settled source/target attempt, large model/dimensions/payload policy, vector checksums, and complete pgvector publication. It refuses unknown origins and differing newer target entries. It can resume/repeat without inference or overwriting an immutable verified entry:

```powershell
# Set RFP_DATABASE_DSN privately to the new empty bidmate_corpus_20261003 target; initialize extension first.
python -m rfp_assistant.migration import --plan "$PWD/.runtime/postgresql-migration/paid-source-plan.json" --batch-size 1000
python -m rfp_assistant.migration validate --plan "$PWD/.runtime/postgresql-migration/paid-source-plan.json"
python tools/import_embedding_cache.py --plan "$PWD/.runtime/postgresql-migration/paid-source-plan.json" --source-runtime "$PWD/.runtime" --index 62bea0c9c27ad3e7 --out "$PWD/.runtime/postgresql-migration/paid-pgvector-transfer.json"
```

The actual transfer completed: 18,548 verified payloads, 18,983 mapped rows and all 79 settled source/target attempts produced PostgreSQL set `p0012817a0267254`. Repeating the full command reused all 18,548 entries, imported zero entries, made zero provider calls and kept the same set identity (`paid-pgvector-repeat.json`). The new PostgreSQL set appends a separate index record after full snapshot parity validation; imported historical rows must remain unchanged.

Post-publication verification passed canonical digests for all 27 imported tables / 646,033 records, excluding only the newly appended PostgreSQL set's index record. All 18,983 stored vector rows are byte-identical float32 values to the preserved construction matrix. Ten exact top-20 comparisons (five corpus-row vectors, each against the full population and its extraction scope) produced identical ordered results, with score differences within 1e-6. This verifies storage/ranking parity for available new vectors; corpus-row probes do not establish query quality or old-model parity. PostgreSQL and live ledger totals agree at $1.631473, with zero pending reservations and target paid admission disabled. Private receipt: `paid-transfer-verification.json`.

Observed `pg_total_relation_size` values, including table/TOAST/index storage, are 177,840,128 bytes for `embedding_payloads`, 32,768 bytes for `embedding_sets`, and 8,503,296 bytes for `embedding_set_rows`. These are construction-state measurements, not concurrency/latency benchmarks. The focused real-PostgreSQL transfer test covers repeated publication with no dispatch and rejection of a cache lacking a matching settled source attempt.

Quality reference/paired shortening, evaluation query caches, benchmarks, production activation/cutover and post-write rollback acceptance remain outstanding. No manual review was restarted.

## Review round 1 disposition

F1's documentation observation is reproducible, but its proposed return to the ladder conflicts with the owner's later explicit 1,536 instruction. The owner also expressly authorized paid corpus construction before label freezing. Neither instruction approves activation or changes quality thresholds. `spec-update.json` records the full revision proposal against revision 2, preserving all 26 completion conditions while updating these requirements and the approved $10 editable cap. The server must apply the fenced revision; task metadata was not manually altered. Candidate choice, paid construction and quality acceptance are separate. There is no claim of smallest-passing selection or completed acceptance. The dimension choice also applies to defaults/configuration, corpus/query dispatch, cache/set identities, estimates and future activation; these retain 1,536 rather than reintroducing the superseded ladder.

F2 reproduced in two independent paths: the reviewed launcher replaced an explicit DSN with `bidmate_rehearsal` in an isolated PowerShell fixture, and real-PostgreSQL `Resources` initialized an empty target instead of refusing it. The launcher now starts infrastructure only and leaves both an existing DSN and an unset DSN unchanged. A shared import check runs before schema writes in both application `Resources` and ordinary maintenance CLI startup. It requires the complete import marker produced only after canonical import checks. Empty and partial imports are refused; rejection leaves an empty target eligible for subsequent import. Explicit `init` remains a separate non-migration action. Scope counted separately: one infrastructure launcher, two application/maintenance entry points, and three launch/setup documents (root README, runbook, this handover). Compose's `bidmate_rehearsal` remains an infrastructure/healthcheck database, not an implicit serving target. Production instructions explicitly retain SQLite until coordinated cutover.

An additional real FastAPI lifespan check refused extension-only database `bidmate_startup_f2_20261004` with application table counts 0 before and 0 after; no SDK/provider dispatch occurred. The first attempt to use `bidmate_rehearsal` for this extra check stopped at its read-only precondition because that database already contained tables; it was preserved, and no application startup was attempted against it. The integration check also proves successful import after rejected startup, refusal of a partial import and successful `Resources` creation after a complete import.

F3 reproduced with the CLI-format test and with the actual documented PostgreSQL CLI command. The pre-fix isolated restore completed its checks, wrote its receipt, then raised `KeyError: 'staging'` and exited 1. The CLI now requires `passed`/`checks` and includes `staging` only when the backend returns that filesystem path. After the repair, the actual command restored into isolated `bidmate_restore_f3_after_20261004`, returned 0 and printed all 19 checks as true, with paid admission disabled and zero provider calls. Scope counted separately: one CLI report consumer, two backend report producers (SQLite directory and PostgreSQL database), and one verification walkthrough calling that CLI. The pass/fail test covers both formats and both exit statuses. Managed immutable references remain at their original locations; this does not close the outstanding relocation/post-write rollback requirements.

Focused verification commands for these repairs:

```powershell
# Set RFP_POSTGRES_TEST_DSN privately to the isolated bidmate_tests database, extension only.
python -m unittest tests.test_postgres tests.test_release tests.test_postgresql_launcher tests.test_api tests.test_service -q
python -m unittest tests.test_generation tests.test_dense tests.test_evaluation tests.test_retrieval tests.test_shell -q
# The settings environment selects the imported source and a distinct empty restore target privately.
python -m rfp_assistant.cli restore-check --backup <absolute-postgresql-backup-manifest>
```

No paid evaluation, activation, maintenance cutover or independent-review dispatch was performed in this repair round.

The first scoped command passed 82 tests in 309.957 seconds with one skip; the remaining shared-resource callers passed 124 tests in 395.554 seconds. The earlier focused pre-fix run failed with the expected empty-startup assertion and PostgreSQL `staging` exception; the post-fix focused run passed. All 11 changed files validate as UTF-8 without BOM, and the complete revision proposal retains the repository gate. The separate actual PostgreSQL restore command returned exit code 0, not only a Python API report.

## Recovery review repair (2026-10-04)

F1 reproduced with a native CLI restore, a valid fixture backup, an empty restore database and an unreachable primary DSN. Before repair, the command failed opening the primary pool and never restored the target. The first fixture attempt failed on its path configuration; correcting that fixture reproduced the reported pool failure. Recovery now dispatches before primary initialization. Scope counted separately: one shared CLI startup path, one PostgreSQL backend source-DSN comparison, and two backup-format dispatch paths (PostgreSQL database and SQLite directory). The source comparison uses configuration only. Target pool parameters are copied into historical-index settings so a primary checkout timeout cannot select a different unowned target pool.

F2 reproduced by completing native `pg_restore` and injecting an exception before admission cleanup: the original target retained copied `paid_admission=true`. Recovery now has a durable fence independent of dumped controls and an atomic native restore. Scope counted separately: one dump/restore pair, two startup consumers (Resources and ordinary CLI), one migration-import entry, and three paid enforcement boundaries (gateway-owner check, durable reservation and durable dispatch). All reject a fenced target. Native tests exercise a real SQL restore error, an exception after copied controls, and termination of the lock-owning PostgreSQL session during verification. Copied complete/admission flags cannot permit spending, and ownership loss cannot publish readiness. Both advisory locks remain held through checks and receipt publication.

F3 reproduced by deleting a referenced artifact after a completed import: validation returned false but the original startup guard accepted the target. Scope counted separately: beyond the reported startup guard, two entry points consume it, two paths publish validation (migration and recovery), and three artifact categories require checks (originals, extractions and index manifests with their payload files). The three paid enforcement boundaries counted under F2 also require validation. Persisted validation is bound to the imported snapshot, invalidated before checks and required for startup and paid admission. Migration validation requires the unchanged imported plan, so removing its artifact list cannot bypass checks. Startup rechecks hashes to catch file loss after successful validation. Tests cover completed imports without validation, a changed plan omitting artifacts, failed validation, corruption after validation, missing originals, corrupt index payloads, repaired files requiring revalidation, and failed restore artifact checks.

The recovery tests create fresh `bidmate_recovery_test_*` databases, perform native dumps/restores and drop only those test databases. They use fake inference and make no provider calls. Run with the private isolated test DSN:

```powershell
python -m unittest tests.test_postgres tests.test_postgresql_recovery tests.test_release tests.test_generation tests.test_dense -q
```

These repairs do not activate the reduced-large set or perform a production cutover. Managed artifact relocation and rollback after new spending remain outstanding as listed above.

The full rehearsal backup was also restored into fresh isolated `bidmate_restore_disaster_20261004` with `RFP_DATABASE_DSN` pointing at unreachable loopback port 1. The native CLI returned exit code 0 and all 19 checks true, covering 27 tables, 645,947 records, 733 immutable references and 14 copied files. No source connection or provider call was required. The restored database remains paid-disabled; this historical snapshot is recovery evidence, not the current live ledger or a serving cutover.

Final PostgreSQL/recovery verification passed 26 tests in 107.213 seconds. The initial combined run passed the 57 release/generation/dense tests but failed one PostgreSQL fixture helper: it unnecessarily revalidated an old snapshot after the repeat-import test deliberately added newer records. The helper now validates only a newly completed import; the final PostgreSQL rerun passes the resume/repeat case without overwriting newer state. Across these scoped runs, all 83 distinct tests pass. `git diff --check` and UTF-8 without BOM checks passed for the changed files.

## Review round 2 receipt repair (superseded by round 3)

F4 reproduced by terminating the native PostgreSQL lock-owning session during validation: the command raised a database error and the target remained fenced, but `postgresql-restore-check.json` still contained `passed: true`. The added receipt assertion failed before repair. Success receipt publication now follows ownership verification, the committed readiness/validation transaction and target pool shutdown. The gateway/import locks remain held during publication. A new attempt clears the previous latest receipt before connecting to the target, so an interrupted retry cannot leave an earlier success as its result. A receipt-write failure still fences the target and leaves no success receipt.

Scope counted separately: one reported PostgreSQL receipt writer, three other restore outputs in the SQLite backend (staging report, backup-side report and release summary), and one shared CLI result consumer. SQLite already completes validation and conservative recovery before writing its reports; there is no later database readiness commit in that path. Its ordering and the CLI pass/fail mapping need no change. The two backup manifest writers were also inspected: both publish after completing their snapshots; SQLite's subsequent audit write does not govern restored-target readiness. No additional receipt-before-readiness defect was found.

Focused native verification uses `python -m unittest tests.test_postgresql_recovery -q` with the isolated test DSN. All nine tests passed in 68.548 seconds. The session-loss test also seeds a previous success receipt and confirms it is removed. A successful-publication test queries committed readiness through an independent PostgreSQL connection at the receipt write; a write-failure test confirms target fencing and absence of a success receipt. No provider calls or production changes were made.

After placing publication outside the target pool lifecycle, the three affected session-loss, committed-publication and receipt-write-failure tests passed again in 22.379 seconds. `git diff --check` and UTF-8 without BOM validation passed for all three changed files.

## Review round 3 atomic recovery publication

F2 reproduced in a native restore child process paused at the old filesystem receipt writer. An independent connection accepted startup before any receipt was written. The test failed with `RuntimeError not raised`; terminating that child bypassed Python cleanup. The first fixture attempt stopped because its Settings constructor omitted `hwp_converter`; correcting that fixture reproduced the actual startup gap.

Receipt publication and readiness now share one durable PostgreSQL transaction. The authoritative receipt is stored in the isolated target's recovery schema, with the check timestamp and backup dump hash. No filesystem success export follows the commit. Before commit, other connections see the old closed fence and no receipt; abrupt process loss rolls back publication and leaves the target blocked. After commit, both the successful receipt and readiness exist. Failed checks commit a failed receipt with a failed fence. Exceptions during publication roll back both. This replaces the cross-store ordering from round 2 and preserves its protection against success receipts on failed recovery.

Scope counted separately: one reported publication transaction, two startup consumers (Resources and ordinary CLI), three paid boundaries (owner check, reservation and dispatch), and two migration entry points (import and validation) share the recovery guard. Each requires a committed successful receipt for a verified recovery fence. The three SQLite report outputs already follow completed validation/recovery and have no separate restored-target readiness transaction. The shared CLI consumer now reports the PostgreSQL receipt location. Two operator documents were updated: this handover and the runbook.

The native tests pause immediately after inserting the receipt inside the publication transaction, inspect the target from independent connections, and forcibly terminate only the test child process. Startup is refused before and after termination, and the uncommitted receipt is absent. Successful publication exposes the exact returned report and readiness together; removing the receipt closes startup and admission again. An exception after inserting the receipt leaves no committed success. Backups of verified recovery targets exclude both the old fence and its receipt. The focused recovery file passed ten tests in 64.814 seconds before the final pool-lifecycle placement; final shared-guard verification is recorded below. No provider calls or production cutover were performed.

Final verification: `python -m unittest tests.test_postgresql_recovery tests.test_postgres tests.test_release -q` passed all 38 tests in 181.960 seconds with the real isolated PostgreSQL DSN. `git diff --check` and UTF-8 without BOM checks passed. The generated wiki corpus includes the updated runbook character count.

## Production cutover request and quality gate (2026-10-04)

Production cutover means moving the running application's authoritative records, billing ledger and accepted retrieval set to PostgreSQL together. The owner requested this operation and reaffirmed fixed 1,536-dimensional corpus/query embeddings. No dimension-selection ladder was run. The 3,072-dimensional reference below is an offline exact-search control, never a serving vector set or ANN index.

The recovered 50-question development pilot's exact candidate/review records were copied from the separate historical archive after conflict checks and current source validation. Its ledger, requests, spending and sealed records were not copied. Current family assignments were preserved. A consistent live backup was taken before those additions. Six later-page slots were drafted by `api-gpt-6-luna`; five labels were accepted by `codex-cutover-original-reviewer` after inspecting seven rendered original PDF pages, checking hashes, exact coordinates, conditions and table associations. Unrelated trailing table content was removed from supporting coordinates before freeze, preserving model questions, answers and required claims. A ratio written as a fraction failed the existing numeric label validator and remains excluded with its failed materialization recorded; that validator was not weakened. Two invalid source-index drafts were corrected through a separate, metered model response. No label was adjusted after seeing retrieval outcomes.

The frozen population is explicitly a pilot: 55 questions over nine scoped extraction revisions, including exact identifiers, numeric conditions, multiple passages and document comparisons. Source positions are 12 early, 38 middle and five late. It does not meet every full-gold target. Dataset SHA256: `a3b4d5cc2d524754aa0f902ad8f6426b1c6419a249f23d52e424c8ed1b570419`; evaluated population SHA256: `3566ff2fd18831b0a669c613b04c27eb75073f64eab78b6837dff85fbdab441d`. The dataset, review history and family map were frozen before any comparison query dispatch. All 55 rows remained eligible; sealed questions were not opened for selection.

The bounded reference embedded 2,255 unique exact payloads covering all 2,311 association-specific development chunk rows, with 896,461 input tokens across ten settled batches. Every corpus and query call used the sole live SQLite gateway, the registered large-model rates and existing purpose envelopes. Fifty-five queries were embedded at each of 1,536 and 3,072 dimensions. The first combined reference/query estimate was $0.118038; all 120 embedding attempts settled for $0.117928. Three drafting calls settled for $0.003260. Total new work was $0.121188, bringing cumulative spending to $1.752661, with $8.247339 remaining and zero pending/unknown billing.

Eight paired corpus probes compared API-shortened 1,536 outputs with normalized prefixes of independently obtained native-large vectors: minimum cosine was 0.99999988 and maximum absolute coordinate difference was 0.00006018. This is a small observed sample, not a promise of byte equality. Production corpus/query vectors remain API `dimensions=1536` outputs; derived vectors are not mixed into production.

All configurations held the existing source/chunk policies, Kiwi analyzer, exact-code behavior, RRF constant, evidence limits and reranker policy unchanged. nDCG@5 has 48 eligible rows; packed complete support has 55. Cross-document cases remain in the support denominator and do not acquire an invented single-ranking nDCG score.

| Retrieval | nDCG@5 | Packed complete support | New critical failures versus native / lexical |
| --- | --- | --- | --- |
| Current lexical K1 | 0.8168 | 44/55 (80.00%) | Baseline |
| Large 1,536 dense | 0.6547 | 37/55 (67.27%) | 1 / 9 |
| Native-large dense reference | 0.6448 | 38/55 (69.09%) | Reference |
| Large 1,536 hybrid | 0.7696 | 43/55 (78.18%) | 1 / 4 |
| Native-large hybrid reference | 0.7778 | 44/55 (80.00%) | Reference |

Both candidate channels satisfy the allowed nDCG and two-percentage-point support losses versus their native controls. Both fail the zero-new-critical-failure rules. All comparisons had zero wrong-scope candidates and no fallback. Accordingly the candidate failed acceptance; dimensions were not changed, and neither activation nor production cutover occurred. The existing keyword pointer and sole live ledger remain authoritative. PostgreSQL rehearsal copies are older paid-disabled imports and must not be presented as the latest production ledger.

Implemented comparison commands, with the UI stopped and the explicit pre-cutover configuration:

```powershell
$env:RFP_CONFIG_FILE = (Resolve-Path handoff/postgresql-pgvector/config.corpus-before-cutover.example.json).Path
python tools/check_large_quality.py --out "$PWD/.runtime/postgresql-migration/cutover/quality-01"
# Explicit ceiling; all reservations must also fit the existing envelopes and cumulative cap.
python tools/check_large_quality.py --out "$PWD/.runtime/postgresql-migration/cutover/quality-01" --run --max-cost-usd 0.13
# Verified cached repeat: no additional provider calls, same failed acceptance result.
python tools/check_large_quality.py --out "$PWD/.runtime/postgresql-migration/cutover/quality-01" --run --max-cost-usd 0
# Re-measurement on a compatible, unactivated keyword index (see below).
python tools/check_large_quality.py --out "$PWD/.runtime/postgresql-migration/cutover/quality-02" --index-version 29f261abafeb1f8c --run --max-cost-usd 0.01
python -m unittest tests.test_large_quality -q
```

The command returns 1 for failed acceptance. It refuses changed/unreviewed development inputs, a different serving model/dimension, an incomplete candidate, unknown embedding billing, insufficient envelopes and a ceiling below its cache-aware estimate. Reference searches refuse rows outside the declared development coverage. The first comparison attempt exposed a deduplication bug in the new offline reference assembler: payload deduplication had dropped distinct chunk associations. The mapping now retains every association, with a focused regression check. Reruns reused all settled vectors without repeating paid work.

Quality review round 1 added three pre-dispatch refusals. A keyword index built under another analyzer or query policy is rejected through `retrieval.index_compatibility` (the standard evaluator already did this). Every frozen passage row must have each of its scopes in the active keyword index. A scope missing from the index ranks empty for candidate, reference and lexical alike, so it could pass without being measured. The paired-shortening probe now reads its eight candidate vectors from the verified ready set: the SQLite matrix, or pgvector rows with checksums. It no longer reads the mutable cache, so a cache gap cannot crash the run after paid reference batches. Review round 2 bound the cost plan to the ledger rate. Plans now price corpus and query work with the `budget_settings` rate snapshot that reservations enforce, record its `rate_version`, and refuse a model with no configured rate. The live ledger rate equals the code default (`input 0.13`, rate version `openai-standard-text-embedding-3-large-2026-10-03`), so the recorded estimates and ceilings are unaffected.

A free plan-only rerun against the live data found that the analyzer guard applies to the recorded result. The 2026-10-04 comparison used keyword index `62bea0c9c27ad3e7` (manifest `a1ea4e92…`). `index_compatibility` rejects that index because it predates the frozen title/institution snapshot. Serving marks it `index_outdated` too. The frozen scopes, however, pass the new guard: all 9 scopes of the 55 rows are indexed, with zero reference misses. The table above is therefore a record of what was run, not a valid acceptance measurement.

The comparison was re-measured on 2026-10-04 without moving the serving pointer:

- `build-keyword --include-unreviewed --no-activate` built index `29f261abafeb1f8c` (manifest `60ce3b43…`). It has the same review scope and structural profile, 98 sources and 18,983 chunks. The active pointer stays `62bea0c9c27ad3e7`.
- `plan-embeddings` produced estimate `745bc0f0a4a3`. `build-dense` then embedded 53 uncached payloads (4,620 micro-USD settled) to build ready set `d0973ac11268198d`.
- `check_large_quality.py --index-version 29f261abafeb1f8c --run --max-cost-usd 0.01` embedded 2 missing reference payloads (96 micro-USD) and needed no new query vectors. The run used the same frozen population (dataset `a3b4d5cc…`, 55 rows).

The report is in `quality-02`. Both channels still fail acceptance:

| Retrieval | nDCG@5 | Packed complete support | New critical failures versus native / lexical |
| --- | --- | --- | --- |
| Current lexical K1 | 0.9405 | 53/55 (96.36%) | Baseline |
| Large 1,536 dense | 0.6547 | 37/55 (67.27%) | 1 / 16 |
| Native-large dense reference | 0.6448 | 38/55 (69.09%) | Reference |
| Large 1,536 hybrid | 0.8863 | 53/55 (96.36%) | 0 / 2 |
| Native-large hybrid reference | 0.8886 | 53/55 (96.36%) | Reference |

The compatible index mainly lifts the lexical baseline: K1 rises from 0.8168 to 0.9405. Hybrid now matches its native control on every native check, with zero new critical failures. It still fails the zero-new-critical-failure rule against K1 on `refresh50-b-training-handover` and `refresh50-eg-input-error`. Dense keeps one new failure versus native (`refresh50-b-quality`) and 16 versus K1. All comparisons had zero wrong-scope candidates and no fallback. The eight paired shortening probes again gave a minimum cosine of 0.99999988 and a maximum absolute difference of 0.00006018. The candidate remains unaccepted and cutover remains blocked. Activating `29f261abafeb1f8c` for serving is a separate decision; it would also clear the live `index_outdated` limitation.

Private receipts are under `.runtime/postgresql-migration/cutover/`: pre-label backup location, original page render/hash inventory, model calls and invalid materializations, frozen development manifest, quality plan, five trace files and `quality-01/quality.json`. The latter includes separate gates, trace hashes, candidate vector-content hash, measured billing and the original estimate. Source text, labels, vector caches, databases and credentials remain outside Git.

After comparison, provider-free planning captured schema 6, all 27 tables / 646,277 records and 18,983 active chunks, with zero inaccessible or changed referenced artifacts. Snapshot SHA256: `b4724119b642753f3d34489480f3b77766edc346cedce5459ea20635abf68e15`; private plan: `cutover/current-source-plan.json`. This is a preparation snapshot, not a final maintenance-window cutover snapshot. The additional records include reviewed labels and settled comparison attempts; no historic attempt was relabeled or spending reset.

Remaining work includes accepted critical-support behavior at the fixed dimension, old-model comparison, representative storage/concurrency benchmarks, isolated immutable-artifact relocation, tested rollback after new PostgreSQL writes and the coordinated final import/configuration switch. There is no implemented production-switch command to run while these prerequisites remain unresolved. Keep the task incomplete; do not waive the numerical or critical-support gates to make the requested cutover appear complete.

Validation passed 111 tests across the initial quality checks and the existing dense/evaluation/retrieval suites in 383.438 seconds. The final nine focused quality checks also cover the dispatch-time cost ceiling and rejection of a changed serving dimension before dataset work. Cached native/candidate reruns reproduced the failed gates with a zero-dollar invocation ceiling and no new provider calls. All 187 pre-existing attempts compare byte-for-value unchanged against the pre-label backup; 100 documents, 98 originals, 178,049 historical chunks and existing member/session records remain intact. The three original rehearsal targets were inspected directly and retain `paid_admission=false`. Both wiki lints, `git diff --check` and UTF-8 without BOM checks passed. No application server or independent code review was started by this preparation session.
