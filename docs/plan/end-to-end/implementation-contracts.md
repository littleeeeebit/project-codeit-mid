# Shared implementation contracts

Read this with [the overview](0-overview.md) and the phase being implemented. This is the common implementation specification, not existing application code. Phase documents own work order and checks. If repository code appears before a later agent starts, inspect and reuse it; do not scaffold a second service from this document.

## Planned package and ownership

Use an editable Python package named `rfp_assistant`, installed from a root `pyproject.toml`, with `src/rfp_assistant/` as its package directory. Serve the single application through `rfp_assistant.api:app` (uvicorn, one worker): FastAPI routes over `service.py` plus the built Next.js screens from `web/`, whose navigation has the 질문하기, 검증 and 데이터셋 만들기 pages. (Phases 1–4 shipped a Streamlit `app.py`; it was replaced on 2026-10-02 because its layout limits made the screens hard to read.) Implement only modules a phase needs.

| File | Responsibility | First owner |
| --- | --- | --- |
| `settings.py` | Absolute paths, validated limits, model/rate configuration, configuration fingerprint | Phase 1 |
| `contracts.py` | Request/result records and strict answer schema | Phase 1 |
| `store.py` | PostgreSQL connection lifecycle, schema initialization and transactions | Phase 1 |
| `auth.py` | Principal and capability checks; the no-login visitor principal | Phase 1 |
| `ingestion.py` | CSV manifest, converter invocation, HWP/PDF element extraction and review records | Phase 1 |
| `chunking.py` | Structural chunks, exact requirement inventory, source-span mappings | Phase 1 |
| `retrieval.py` | Same analyzer at indexing/query time; scope filters, exact codes, ranked results | Phase 1 |
| `budget.py` | Reservation, settlement, billing recovery, adjustments and snapshots | Phase 1 |
| `generation.py` | Only runtime SDK call site; embedding/generation gateway, payload counting and output validation | Phase 1 |
| `service.py` | Role-declaring public functions, resource ownership, request orchestration | Phase 1 |
| `api.py` | HTTP routes for the screens in `web/`, one `service` function each; the `X-Member` header carries the visitor name; serves the built screens | Phase 1 (as `ui.py` until 2026-10-02) |
| `cli.py` | Explicit local maintenance commands using the same contracts and gateway | Phase 1 |
| `dense.py` | Hash-keyed embedding cache, matrix construction/scoring, optional local reranking | Phase 2 |
| `evaluation.py` | Dataset validation, frozen runs, source-span scoring and reports | Phase 1 pilot; Phase 4 expansion |

Keep `service.py` as orchestration over ordinary functions. No plugins, abstract repository layer, task broker or second API is needed: `api.py` is a thin in-process wrapper, and `web/` is the only frontend. Its build is a static export the same process serves, so there is no Node at run time. A small bounded thread executor (phase 3) keeps network inference out of the request handlers; it calls the same service.

## Settings and runtime files

Resolve defaults from the installed package/repository location, never the process working directory. Explicit path overrides must be absolute. Fail startup for an unreadable source directory, unwritable runtime directory, inconsistent dates, unknown price/model combination, or an unsafe paid configuration.

| Setting | Initial value or rule |
| --- | --- |
| `RFP_SOURCE_DIR` | Repository `원본 데이터`; resolve `data_list.csv` and `files/` beneath it |
| `RFP_DATA_DIR` | Repository `.runtime/`; all indexes, databases and reports derive from it |
| `RFP_CONFIG_FILE` | Optional absolute path to nonsecret operational configuration |
| `RFP_HWP_CONVERTER` | Absolute path to the isolated converter executable; validate before the HWP trial |
| `OPENAI_API_KEY` | Server environment or the git-ignored `.env`; never print or export it |
| `allowance_usd`, `operational_cap_usd` | `20`, `16`; prior spending counts against the cap |
| `project_start`, `project_end` | Required before paid mode; actual dates, not an automatic 28-day forecast |
| `paid_enabled` | False until prior-use reconciliation and rate checks are recorded |
| `generation_model`, `embedding_model` | `gpt-6-luna`, `text-embedding-3-large`; historical small identities retained; owner selected 1,536 dimensions, pending quality acceptance |
| `evidence_target_tokens`, `evidence_max_tokens` | `3000`, `5000`, counted after expansion |
| `generation_max_output_tokens` | `800`; no UI override above the authorized configuration |
| `question_max_characters` | `2000`; comparison initially limited to two selected documents |
| `retrieval_mode`, `reranker_enabled` | `kiwi_bm25`, false until phase 2 selection |

Runtime layout (records and the ledger are in the PostgreSQL database `bidmate_app`, reached through `RFP_DATABASE_DSN`; the runtime directory holds files only):

```text
.runtime/
  extracted/<source_hash>/<parser_fingerprint>/elements.jsonl
  indexes/<index_version>/manifest.json, chunks.jsonl, tokens.jsonl   (vectors live in pgvector)
  datasets/dev-pilot.jsonl, dev.jsonl, families.json, review-log.jsonl
  sealed/test.jsonl, test-manifest.json
  runs/<run_id>/config.json, traces.jsonl, scores.json, review.jsonl, report.md
  releases/<release_id>/manifest.json, report.md, smoke-results.json
```

Keep the entire runtime tree, `.venv/`, converter environment, model downloads and secret files out of Git. Preserve raw input files and pre-existing `.env`/`.gitignore` edits. Add only missing ignore entries. Do not enumerate or display environment secrets in diagnostics. Runtime paths and raw filenames are internal; user downloads resolve through managed IDs.

Write JSON/JSONL and Markdown with `encoding="utf-8"`, `ensure_ascii=False`, and no BOM. Preserve evidence text exactly and keep NFC normalization in a separate search field. JSONL contains one JSON object per physical line with escaped embedded newlines. Write index files in a temporary sibling directory, verify hashes, then publish a new immutable version and transactionally switch the active pointer. Never overwrite the current matrix in place.

## Identity, metadata and evidence

| Record | Identity and required fields |
| --- | --- |
| Document association | `doc_id`: UUID5 under one committed namespace UUID, keyed by NFC-normalized CSV filename. Keep `csv_row_id`, original filename, notice/revision, raw metadata, normalized fields and field-specific provenance. Filename changes require an explicit identity migration |
| Source | `source_hash`: SHA256 of immutable original bytes; format, managed original path and extraction status |
| Extraction revision | `extraction_id`: hash of original hash, converter/parser versions, settings and any recovery artifact hash; preserve old revisions referenced by traces/gold |
| Element | `element_id`: hash of extraction ID and structural path; kind, source order, parent, raw/search text, location and table cells. Locators are stable within this extraction revision |
| Chunk | `chunk_id`: hash of extraction ID, chunker fingerprint, ordered element spans and search payload; mappings include raw-text character offsets or cell references |
| Requirement | Source form, deterministic normalized lookup key, detail/summary classification, element/span targets and surrounding conditions |
| Evidence unit | Request-local `E1`, `E2`, etc., mapped to immutable document/source/extraction/element spans, quote and location; IDs are not URLs supplied by the model |

Deduplicate extraction and content embeddings by original/payload hash, while preserving each document association. Searchable chunk headings come from the original, not a conflicting CSV institution/title. If two associations share bytes, bind a retrieved chunk to the selected association without counting both as separate supporting facts. Original metadata and CSV metadata remain distinct evidence sources.

Map CSV columns explicitly: `공고 번호` → notice string; `공고 차수` → nullable integral revision; `사업명` → title; `사업 금액` → nullable integer KRW plus raw/quality; `발주 기관` → recorded institution; `공개 일자`, `입찰 참여 시작일`, `입찰 참여 마감일` → typed date/timestamp fields; `사업 요약` → unverified browsing hint; `파일형식`, `파일명` → source association; `텍스트` → incomplete preview, excluded from the authoritative index. Read with `csv.DictReader(..., encoding="utf-8-sig")` and retain multiline fields.

Parse amounts with Decimal, rejecting fractional KRW or unsupported text instead of using float. Missing amounts are null; zero is retained with a review flag. Normalize an integral revision such as `0.0` to zero without converting missing to zero. Korean timestamps use `Asia/Seoul`; date-only values retain date precision. SQL range filters exclude unknowns and must not substitute publication time for closing time. Conflicted critical fields do not silently satisfy a filter: show a conflict state or exclude them with a visible reason.

Locations use PDF one-based physical page, optional printed label, bounding box, table/cell coordinates; HWP uses section path, paragraph/table/cell structural path and requirement code. HWP page numbers appear only when a verified native conversion establishes a map. Recovery artifacts retain the original hash, converted hash, method, reviewer and mapping limitations.

## PostgreSQL persistence contract (2026-10-03)

PostgreSQL 18.6 + pgvector 0.8.6 is the only backend; the application database is `bidmate_app`. `postgres.py` owns pinned extension/schema initialization, a refcounted bounded psycopg pool, qmark parameter translation and a session gateway advisory lock. Each operation owns its connection/transaction; inference holds neither. Allowance admission and mutable writes serialize on the application mutex row. A failed PostgreSQL write fails; there is no fallback store. The 2026-10-04 cutover import from the previous database preserved every source table, including retired authentication records.

Tests run on isolated PostgreSQL databases that `tests/fixtures.py` creates per test environment from a per-run template (pgvector installed) on the server started by `tools/start-postgresql.ps1`, or on the server named by `RFP_POSTGRES_TEST_DSN`. They drop their databases afterwards and never touch `bidmate_app`.

BIGINT micro-USD retains exact money. Immutable serialized JSON remains TEXT; C-collated text retains deterministic identity ordering. Foreign keys, original values, canonical digests, nullable zero distinctions and historical attempt price snapshots remain checked. Schema versions are in `schema_migrations`, not PRAGMA. Typed pgvector values have dimension checks; homogeneous embedding sets include source/payload/checksum/model/policy/provenance identities. Scoped exact cosine search in pgvector precedes ranking.

The owner approved a $10 cumulative operating cap and paid migration work. The Settings control may change that cap through the existing budget-admin capability and visitor attribution, without resetting spending/reservations or enabling paid admission. It scales current envelopes and refuses any reduction below committed costs. This supersedes the earlier CLI-only limit-edit contract, while rate registration and unknown billing reconciliation remain maintenance actions.

See the [PostgreSQL handover](../../../handoff/postgresql-pgvector/README.md) for real-database receipts, pinned versions, tested commands, production cutover prerequisites and unfinished quality/recovery acceptance.

## Records and constraints

The tables below live in PostgreSQL; JSON payloads keep nonindexed structured details compact as TEXT. Schema versions are recorded in `schema_migrations`. Foreign keys are enforced. Every connection comes from the bounded pool with lock and statement timeouts and is returned when its transaction ends.

| Table | Key and essential columns/constraints |
| --- | --- |
| `documents` | `doc_id TEXT PRIMARY KEY`, `csv_row_id`, `filename UNIQUE`, `active_source_hash`, `raw_metadata_json`, `normalized_metadata_json`, `quality_json` |
| `sources` | `source_hash TEXT PRIMARY KEY`, `format`, `original_path`, `active_extraction_id`, `parse_status`, `review_status`, `warnings_json` |
| `extractions` | `extraction_id TEXT PRIMARY KEY`, `source_hash` FK, `parser_fingerprint`, `artifact_path`, `created_at`, `recovery_json` |
| `elements` | Composite key `(extraction_id, element_id)`; source order, kind, parent, raw/search text, location/table JSON |
| `chunks` | Composite key `(index_version, chunk_id)`; extraction ID, source-span mapping, payload, token count, type and requirement key |
| `indexes` | `index_version TEXT PRIMARY KEY`, manifest path/hash, source-set hash, config JSON, state `building`, `ready` or `failed`; active version stored in application settings |
| `reviews` | UUID key, source/extraction ID, reviewer, inspected locations, checks/findings JSON, timestamp; append-only |
| `requests` | UUID request ID, principal/member, idempotency key, input/config hashes, scope snapshot, status, trace/result JSON, timestamps; unique `(member_id, idempotency_key)` |
| `attempts` | UUID attempt ID, request FK, stage/purpose, model, state, `reserved_micro_usd >= 0`, nullable settled cost, raw usage, price snapshot, response ID, timestamps/error JSON |
| `adjustments` | UUID key, signed microdollar amount, closed interval/scope, evidence, reason, actor; unique reconciliation ID; append-only |
| `budget_settings` | Single allowance identity, total/cap, dates, category envelopes, paid-enabled state and change history |

Index sources/elements by source identity and chunks by index, extraction and exact requirement key. Prevent reusing a provider response identity within the same stage/model when present. An embedding response may lack the generation-style response ID; attempt identity remains the settlement key. Do not depend on a provider ID always being present.

`parse_status` is `pending|parsed|quarantined`; review status is `unreviewed|sample_checked|reviewed|needs_recovery`. Fully checked supported coverage is declared explicitly in reports. A syntax success or one inspected cell never implies exhaustive semantic correctness. Failed originals remain discoverable in metadata browsing.

Request status is `queued|running|completed|failed|cancelled|interrupted`; generated domain status and billing state remain separate. Phase 3 adds bounded background submission and request-status reads around the same synchronous worker function.

## Service entry points

Implement these names and semantics so UI and CLI authors can connect without guessing. Typed records may be dataclasses; strict answer models may use Pydantic already required by the SDK. Mutable UI state does not enter shared records.

```text
search_projects(principal, filters, query, limit=20) -> SearchResult
retrieve(principal, question, scope, config) -> RetrievalResult
answer(principal, request) -> AnswerResult
open_evidence(principal, request_id, evidence_id) -> EvidenceView
original_download(principal, doc_id, source_hash) -> ManagedDownload
budget_snapshot(principal) -> BudgetSnapshot
reserve(principal, request_id, attempt_id, stage, payload, price_snapshot) -> Reservation
settle(attempt_id, provider_usage, response_identity) -> Settlement
reconcile(principal, reconciliation_record) -> BudgetSnapshot
```

`AnswerRequest` requires `idempotency_key`, `generation_id`, question, selected `(doc_id, source_hash)` pairs, mode `single|compare|metadata|inventory`, as-of date and requested config ID. The server resolves/restricts that config and captures the immutable active index/prompt/rate snapshot. A repeated key with the same input hash returns the existing request/result; the same key with different input is a conflict. A user-requested retry has a new key and an explicit cost estimate.

`RetrievalResult` includes mode/fallback, candidate ranks per channel, exact matches, packed evidence/coverage, parser limitations, token counts, timings and trace ID. `AnswerResult` includes domain status, claims, missing/conflicting data, evidence map, request/attempt IDs and billing state. Technical failure remains a separate outcome even if usage settled successfully.

## Strict generated answer shape

Use the schema below with required fields, explicit enums and `additionalProperties: false` at every object level. Nullable fields remain required with a null union. All user-facing strings are Korean; field names and maintained prompts are English. No generated URL or page field is accepted.

```json
{
  "status": "answered",
  "summary": "Korean conclusion",
  "claims": [{"text": "Korean claim", "kind": "source_fact", "doc_id": "D1", "evidence_ids": ["E1"]}],
  "missing_fields": [{"doc_id": "D1", "field": "closing_time", "reason": "not_found_in_context"}],
  "conflicts": [{"field": "institution", "alternatives": [{"doc_id": "D1", "value": "Korean institution", "evidence_ids": ["E2"]}]}],
  "next_action": null
}
```

Statuses: `answered|insufficient_evidence|clarification_required|conflicting_evidence`. Claim kinds: `source_fact|inference`; inference requires supporting evidence and must be labeled visibly. Missing reasons: `unknown_metadata|not_found_in_context|ingestion_unavailable|source_absence_verified|scope_ambiguous`. Only independently established absence may use `source_absence_verified`; top-k misses cannot establish it.

Server checks include allowlisted evidence IDs; matching claim document/source scope; actual stored quotes/spans; nonempty references for material claims; evidence-backed conflict alternatives; all compared documents represented by claims or limitations. Reject unknown fields and invented evidence targets. Refusal, truncated output, malformed JSON and provider errors are technical outcomes, not domain abstention. Settle any reported usage before reporting an invalid answer. Structured shape cannot prove entailment; independent evaluation still owns factual support. [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) documents schema constraints and separate refusal handling.

## Budget state machine

```text
reserved -> dispatching -> settled
                     \-> unknown -> settled or reconciled
reconciled -> settled (late usage with atomic compensation)
reserved -> released  (dispatch never began, conclusively)
dispatching -> released (confirmed provider rejection before execution only)
```

Mark `dispatching` durably before the network call. Crash between this write and actual dispatch is conservatively unknown on recovery. `reserved` with no dispatch marker can be released after exclusive recovery confirms no old worker can dispatch it. Never retry an unknown attempt automatically. Final usage may settle a row from `dispatching`, `unknown` or `reconciled` exactly once; a `reconciled` row requires the compensating adjustment below.

Costs are integer microdollars, conservatively rounded after computing each attempt with Decimal rates. Settled cost counts uncached input, cached input and output once; embeddings use reported input only. Category totals and the global cap update in the same transaction.

```text
spent = sum(settled attempt costs) + sum(signed adjustments)
pending = sum(reservations for reserved, dispatching and unknown attempts)
available = operational_cap - spent - pending
admit iff paid_enabled and available >= proposed_reservation
```

Inside one transaction holding the application mutex row (`SELECT … FROM application_mutex … FOR UPDATE`), check idempotency, configured model/rates, global/category remainder, and insert the reservation. Commit before dispatch. Settle in another short transaction, replacing the reservation and incrementing a ledger revision. Negative external adjustments require owner evidence and cannot erase unresolved attempts casually. Preserve original token counts and correction history.

Count all serialized generation content, including schema and message framing, with the model tokenizer. Start with an explicit conservative framing margin, record it, and compare local counts with reported usage in the first bounded call; it is an estimate, not a provider guarantee. Reserve uncached input plus the output cap; if reported cost exceeds the reservation, record the true cost, freeze new paid work, and inspect counting/rates before resuming. The $4 reserve and verified provider controls absorb uncertainty; do not claim exact prevention of all external billing overruns.

The shared SDK client has retries disabled and a finite timeout. Creation, concurrent-use decision, controlled closure and crash recovery belong to `service.py`. The [official SDK](https://github.com/openai/openai-python) supports retry configuration and explicit `close()`; verify concurrency with the pinned runtime rather than assuming safety. All OpenAI imports/calls stay in `generation.py`, with a test seam for transport substitution. A fake transport must never require a real key.

Reconciliation records a nonoverlapping closed interval and matching provider project scope. Compute provider total minus already-settled local cost for that interval, record the adjustment and watermark atomically, and add later local costs normally. Store explicitly covered attempt IDs and evidence that the provider's finalized interval covers them; only those unknown attempts move to `reconciled` and release their reservation. Boundary-spanning or otherwise unconfirmed attempts retain conservative pending reserves, visibly distinct from settled spending.

When final usage arrives for an explicitly covered `reconciled` attempt, atomically transition it to `settled`, store its raw usage and measured cost, and append a compensating negative adjustment of that cost: the provider reconciliation already counted it. Use a unique correction identity such as `late-settlement:<attempt_id>` so duplicate completion cannot apply compensation twice. Preserve the original aggregate adjustment and token evidence. Re-importing a reconciliation is harmless; a changed finalized provider total requires a separate owner correction rather than overwriting history.

## Access and local maintenance

There is no login (owner decision 2026-09-30, reaffirmed 2026-10-01). No accounts, tokens, sessions or login screen exist. The UI builds a `Principal` from the name typed in the sidebar (default `owner`) with every capability; the name attributes requests, attempts, reviews, corrections and audit events and is not authentication. Network reach is the only access control: localhost by default, and any wider exposure is an explicit owner deployment decision that lets everyone who can reach the server spend the allowance and use the admin page.

`Principal` still carries `consultant`, `verifier`, `budget_admin` and `sealed_evaluator` capabilities, and every service entry point checks one, including cached reads and file access. Those checks declare each function's role; in-process callers (CLI jobs, tests) may pass narrower principals, and a later login could reuse them. Protections that do not depend on identity stay mandatory: managed `(doc_id, source_hash)` downloads, sealed rows never served to the verifier page, a reason and an audit event for every owner action, and no bulk release of unknown billing. Labels and cost estimates are not client-controlled authority.

Maintenance CLI commands run on the owner-controlled host. Paid CLI work is exclusive maintenance mode: stop the UI, validate the configuration, and reuse the same database, rates and gateway. PostgreSQL holds a dedicated session advisory lock until resource cleanup and refuses a second paid owner across all hosts sharing the database. Lost ownership is terminal and checked again immediately before SDK dispatch. Ordinary team members use verifier actions; no raw-key notebooks or second billing store. CLI fake checks use a temporary data directory and an isolated test database, and never write production state.

## Handoff format

At each phase exit, record implemented files, configuration/schema/index versions, exact commands and outcomes, real/fake execution status, USD spent, reviewed coverage, open failures and the next phase's inputs. Plans describe future checks; only an implementation outcome may claim they passed. A successor reads this contract plus the preceding handoff before changing schema or defaults.
