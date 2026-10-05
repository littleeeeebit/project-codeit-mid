# RFP assistant (입찰메이트)

Internal assistant for historical Korean RFPs: search projects, select one document, ask a scoped question, receive one metered grounded answer, and open the original evidence. The plan lives in [docs/plan/end-to-end](docs/plan/end-to-end/0-overview.md); this README covers setup and launch for what is implemented (phases 1–4). Operations (access, stop/restart, billing recovery, reconciliation, evaluation, sealed run, backup/restore, index rollback) are in the [runbook](docs/operations/runbook.md); screen layout and request ownership in [DESIGN.md](DESIGN.md). Phase outcomes: [phase 2](handoff/phase2/README.md), [phase 3](handoff/phase3/README.md), [phase 4](handoff/phase4/README.md) and the [release report](docs/operations/release-report.md).

## Environment

The tested environment is the conda env `rfp-assistant` (Python 3.12, Windows). `requirements.txt` holds the curated direct pins; `requirements-lock.txt` records every installed version from that environment.

```powershell
conda create -n rfp-assistant python=3.12
conda activate rfp-assistant
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

The HWP converter (`hwp5proc` from pyhwp 0.1b15) is installed into the same environment and resolved automatically from `Scripts\hwp5proc.exe`. To use an isolated converter environment instead, set `RFP_HWP_CONVERTER` to the absolute path of its `hwp5proc.exe`. The converter prints a harmless warning when `xmllint` is missing.

## Database: PostgreSQL only

The application runs only on PostgreSQL 18.6 with pgvector 0.8.6. Every record lives there: documents, extractions, chunks, requests, traces, the billing ledger, audit events and the embedding vectors: the serving set and each compared model's own set, every one tagged with its model, dimensions and prefix policy. There is no other database and no fallback. Start Docker Desktop (Linux containers), then run `./tools/start-postgresql.ps1`. It starts the pinned server on loopback port 55432 and leaves `RFP_DATABASE_DSN` unchanged; it never selects the empty `bidmate_rehearsal` database. The application database is `bidmate_app`. Point `RFP_DATABASE_DSN` at it in the process environment, never in a file the repository tracks.

Startup refuses to run with a clear message when `RFP_DATABASE_DSN` is missing, or when the database has no validated import (the `migration_import` / `migration_validation` marker plus unchanged artifact hashes, index payload files included). A failed validation or an open recovery fence also closes paid admission. Paid dispatch needs both the ledger switch and PostgreSQL paid admission, which `paid on` sets together, plus the database-wide gateway advisory lock that the serving process holds.

The cutover ran on 2026-10-04. With the UI and writes stopped, a final consistent snapshot of the previous database was imported and validated: 27 tables, 674,049 rows, 18,983 active chunks, every referenced artifact hash matching, the ledger and every historical attempt unchanged, and no pending or unknown billing. All 18,983 active vectors are in pgvector, byte-identical to the verified set. Keyword index `29f261abafeb1f8c` is active. Serving is hybrid with keyword-first fusion over exact pgvector search, through run `H-0fffb2a6ec` (measured under the current corpus-routing rule; activation refuses a run measured under another):

- The BM25 top 6 stay in BM25 order, and the rest come from weighted RRF (k 60, dense weight 1.0).
- Each channel retrieves 50 candidates, fusion keeps 50, and 10 evidence units go to the model.
- This is the best setting that passed the fusion gate against keyword-only K1 at the same limits. Among the alternatives measured, 20–30 units and the local reranker failed.
- HNSW did not reach recall@20 0.99 against exact search on scoped questions, so exact search serves.

Ask has an All documents scope across all 98 active sources. A question naming a project is narrowed to that project's documents; generic title words alone, such as 대학교 or 사업, narrow nothing. A question that only names the project gets its overview passages, ranked by meaning. The needle set finds the target passage in the top 5 for 32 of 33 questions (Wilson 95% 0.85–0.99). Measurements, the paid end-to-end check, the archive and the rollback are in the [PostgreSQL handover](handoff/postgresql-pgvector/README.md#postgresql-only-operation-2026-10-04).

The importer and the previous database's backup/rollback paths were deleted after the final import validated. Verified PostgreSQL custom-format dumps are kept under `.runtime/archive/` as cold archives that no code references; the previous database's files were deleted on 2026-10-05. The Phase 4 pilot runtime (gold candidates, sealed set and pilot runs behind the [release report](docs/operations/release-report.md)) lives in its own PostgreSQL database, `bidmate_pilot_archive`, kept apart from the live ledger with paid admission disabled. Rollback means restoring the newest verified dump, taken after the last paid write (ledger revision 1712), into an empty database with `restore-check`. That leaves paid admission off. Records written after the dump's watermark are reconciled before `paid on` ([runbook §12](docs/operations/runbook.md)). The owner approved paid work and a $10 operating cap on 2026-10-03. The Settings page can change the shared cumulative limit while preserving spending and reservations; it uses the existing no-login attribution model.

## Configuration

Settings resolve from the repository location, never the working directory. Optional overrides must be absolute paths.

| Variable | Default | Purpose |
| --- | --- | --- |
| `RFP_SOURCE_DIR` | `<repo>/원본 데이터` | `data_list.csv` and `files/` |
| `RFP_DATA_DIR` | `<repo>/.runtime` | Managed extractions, indexes, datasets and reports; `archive/` holds the cold archives |
| `RFP_DATABASE_DSN` | required | Secret PostgreSQL connection string of the validated application database, in the process environment |
| `RFP_POSTGRES_TEST_DSN` | local server | Tests and `check`: a server where the test user may create one isolated database per environment |
| `RFP_RESTORE_DATABASE_DSN` | none | `restore-check` only: an empty isolated database to restore into |
| `RFP_CONFIG_FILE` | none | JSON with nonsecret `Settings` fields (unknown keys are rejected) |
| `RFP_HWP_CONVERTER` | env `Scripts\hwp5proc.exe` | HWP → XML converter |
| `OPENAI_API_KEY` | none | Process environment, then the repository `.env`; never printed |
| `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | none | Langfuse tracing; read like the API key. Any one missing turns tracing off |

Without `OPENAI_API_KEY` the app still runs: search, filters, evidence browsing and retrieval traces are free; paid generation reports that the provider is unavailable. `provider: "fake"` in the config file never builds a real SDK client.

### Langfuse tracing

A local Langfuse v4 stack can record every `/api/ask` request and every drafting run. A request trace shows retrieval (chunk IDs, scores and text), the assembled evidence, the `gpt-6-luna` generation with its usage and ledger cost, and the answer validation. It carries three free, deterministic scores from the existing checks: `citation_valid`, `insufficient_evidence` and `evidence_tokens`. Embeddings, ingestion, OCR and evaluation runs are not traced. Tracing is observability only: admission, reservations and settlement never read it, and an absent, stopped or failing Langfuse changes neither answers nor the ledger.

Start Docker Desktop, then run `./tools/start-langfuse.ps1`. The first run generates the stack's secrets into the gitignored `.runtime/langfuse.env`. It creates the organization, the project and its API keys headlessly, and starts `compose.langfuse.yaml` detached (Compose project `bidmate-langfuse`, with its own Postgres, ClickHouse, Redis and MinIO). Later runs reuse the same secrets and volumes. The script also writes the three `LANGFUSE_*` lines into `.env` and removes any `LANGFUSE_BASE_URL` there, which would point SDK tools elsewhere. Tracing starts with the next application start.

The UI is at <http://127.0.0.2:3100> (MinIO media at port 9190). Both listen only on `127.0.0.2`, a loopback address of their own: browsers share cookies across ports, so a second local Langfuse on `127.0.0.1` would otherwise break this one's sign-in. Use that address rather than `localhost`, which a browser may resolve to `::1`. Sign in with `LANGFUSE_INIT_USER_EMAIL` and `LANGFUSE_INIT_USER_PASSWORD` from `.runtime/langfuse.env`. A request's trace ID is derived from its request ID (`Langfuse.create_trace_id(seed=request_id)`). Text is sent in full, except that one mask on the client redacts API keys, tokens and other secret-shaped values on export.

To turn tracing off, remove the three `LANGFUSE_*` lines from `.env` (and from the process environment) and restart the application: no Langfuse client is created. Stop the stack with `docker compose --env-file .runtime/langfuse.env -f compose.langfuse.yaml down`; the secrets live only in that file, so Compose cannot read the stack definition without it. Add `-v` only to delete its data, and then delete `.runtime/langfuse.env` as well.

Answers use `gpt-6-luna` through Chat Completions with strict structured output, `reasoning_effort: low` and a default maximum of 4,000 output tokens, reasoning included: the phase-4 pilot's cap, restored on 2026-10-05 after a 2,000-token cap left a comparison no room past its reasoning. Its results and limitations are in the [release report](docs/operations/release-report.md). Rates are in `settings.DEFAULT_RATES`: standard tier, $0.10 input, $0.01 cached, $0.125 cache write and $0.50 output per 1M tokens. Evidence is capped per document by the serving run's limits (4,800 tokens), far below the 272K long-context tier.

## Commands

Run from any directory with the environment's interpreter:

Every command except `check`, `load-check` and `restore-check` runs against `RFP_DATABASE_DSN` and refuses a database without a validated import. Every startup refuses an embedding model outside the compared registry (`models.EMBEDDINGS`) or at dimensions other than the ones it produces; serving then uses whatever embedding the activated run evaluated. Every connection, pooled, the paid gateway's or `restore-check`'s, refuses a server other than PostgreSQL 18.6, and pooled connections refuse pgvector other than 0.8.6. `check` and `load-check` use temporary databases on the server named by `RFP_POSTGRES_TEST_DSN` (default: the local server from `tools/start-postgresql.ps1`), and `restore-check` uses `RFP_RESTORE_DATABASE_DSN`. `init` creates the schema in a database the application owns; it never resets spending.

```powershell
python -m rfp_assistant.cli init --paid-disabled          # schema + allowance row; never resets spending
python -m rfp_assistant.cli manifest                      # all 100 CSV associations, hashes, duplicates, conflicts
python -m rfp_assistant.cli ingest                        # every original; failures are quarantined with a reason
python -m rfp_assistant.cli build-keyword --include-unreviewed   # operating index over every parsed source
python -m rfp_assistant.cli check --phase 1 --provider fake   # automated invariants in temporary state, no key
python -m rfp_assistant.cli check --phase 3 --provider fake   # service, budget and request-state gate
python -m rfp_assistant.cli load-check --users 6 --provider fake   # six concurrent members, temporary ledger
python -m rfp_assistant.cli check --phase 4 --provider fake   # gold, evaluation, sealed run, backup and report gate
python -m rfp_assistant.cli check --phase all --provider fake --save   # every test; recorded for release-report
```

The screens are a Next.js app in `web/` over a FastAPI wrapper of `service` (`src/rfp_assistant/api.py`). A build is
a static export that the API serves itself, so one process on one port serves both. Node.js is needed only to build:

```powershell
cd web; npm ci; npm run build; cd ..                     # writes web/out (types come from web/openapi.json)
$env:RFP_CONFIG_FILE = (Resolve-Path handoff/postgresql-pgvector/config.example.json).Path   # RFP_DATABASE_DSN set
python -m uvicorn rfp_assistant.api:app --host 127.0.0.1 --port 8501 --workers 1
```

Exactly one worker: paid requests run on the process's own executor and the process holds a database-wide gateway advisory lock. For screen
work, run the API on 8511 and `npm run dev` in `web/` (port 8510, `/api/*` forwarded to `RFP_API_URL`, default
`http://127.0.0.1:8511`). After changing a route or its shapes, regenerate the schema the screens are typed from with
`python tools/openapi.py`; `tests/test_api.py` fails while it is stale. `npm run lint` and `npm run typecheck` check
`web/`.

For local work, use a branch in the original checkout rather than copying the repository. The original corpus,
`.env` and runtime are ignored by Git and do not appear in a linked worktree. If a task already requires a
worktree, set `RFP_SOURCE_DIR` and `RFP_DATA_DIR` to the existing corpus and shared runtime before starting the
server, with permission to write to that runtime. Keep the API key in the server environment. Never copy the
budget DB to create a second independently spendable ledger. The shared runtime still has exactly one gateway
owner.

A fixture server with `FakeTransport` is for isolated UI or invariant checks. It is not the running application
for user acceptance and cannot establish real retrieval or model-answer quality.

The operating index includes sources whose extraction has not been compared with the original yet; the consultant screen and every trace label them. `build-keyword --reviewed-only` builds the stricter index from sources that passed the automatic check (`auto_verified`) or a human review; the operating index also keeps `unreviewed` and `auto_flagged` sources, labeled.

### Fidelity check against the original

Nobody reads whole documents. For each HWP, `fidelity run` has Hancom Viewer print the original to PDF and answers the save dialog itself (the Windows default printer must be "Microsoft Print to PDF"). The printed PDF's text layer comes from Hancom's own renderer, not from pyhwp, so it is an independent witness. The check then compares both ways:

- every extracted paragraph and table-cell line must appear in the rendering (6-character shingles, so a single wrong character is reported);
- every rendered line and ruled-table cell must appear in the extraction;
- runs of 3+ digits must match exactly.

The verdict is `auto_verified` (자동 대조 통과) or `auto_flagged` (자동 대조: 확인 필요), with the exact element, table cell and page of every difference. On the verification screen, `원문 대조 (HWP)` lists the documents; a person opens only the reported places next to the printed page and can mark the document `sample_checked`. A human review status is never overwritten by an automatic one. Text inside images is in neither side, so pages with images are listed separately.

```powershell
python -m rfp_assistant.cli fidelity run                  # every parsed HWP; reuses existing prints (--reprint to redo)
python -m rfp_assistant.cli fidelity run --doc-id <id>
python -m rfp_assistant.cli fidelity show --doc-id <id>   # metrics and findings
python -m rfp_assistant.cli review render --doc-id <id>   # one PNG per page under .runtime/reviews/rendered/<hash>/
python -m rfp_assistant.cli review record --doc-id <id> --reviewer <name> --status sample_checked --locations loc.json --findings findings.json
```

Printing takes 20–30 s per document and shows viewer windows. Do not print anything else while it runs. The prints are kept in `.runtime/reviews/printed/<hash16>.pdf`, and the originals are never modified.

### Text inside images

`ocr` reads every raster image region (HWP print, or PDF original) with PaddleOCR-VL on the GPU. Only regions that fail the fallback test (loop, low confidence, or output length far from what the ink suggests) are re-read by `gemini-3.5-flash-lite`, under a $0.50 cap of its own recorded in `.runtime/ocr/gemini-ledger.jsonl` (needs `GEMINI_API_KEY` in `.env`). Results are cached per region under `.runtime/ocr/<hash>/`, and a rerun resumes. The next `ingest` places each region's text as an `image_text` element next to the print text around the picture. It is chunked apart and cited as OCR, and the fidelity check skips it. Design and calibration: [docs/rag/preprocessing.md](docs/rag/preprocessing.md#text-inside-images).

```powershell
python -m rfp_assistant.cli ocr --local-only              # free pass: local reads, flagged regions stay unresolved
python -m rfp_assistant.cli ocr                           # Gemini for the flagged regions, within the cap
python -m rfp_assistant.cli ingest                        # merge OCR text; then fidelity run and build-keyword again
```

### Dataset questions

Drafted questions wait in a review queue. On the `데이터셋 만들기` page a verifier picks development documents and source passages, has gpt-6-luna draft source-bound questions within a consented maximum (charged to `gold_eval`), and sends the valid drafts to the queue. A reviewer whose typed name differs from both the drafter and whoever started the drafting run then approves each one into the dataset or rejects it into the rejection wiki, always with a note, with the draft shown next to its original passages. The drafting procedure for agents, including exact file locations, is [.wiki/gold-drafting.md](.wiki/gold-drafting.md).

```powershell
python -m rfp_assistant.cli gold status                   # counts, rejection wiki path, rejections awaiting an inferred reason
python -m rfp_assistant.cli gold submit --file batch.jsonl --batch <id> --dataset dev-pilot --drafted-by <agent>
python -m rfp_assistant.cli gold infer --candidate-id <id> --by <agent> --file inference.json
python -m rfp_assistant.cli gold check                    # dataset file and wiki pages equal their database rendering
python -m rfp_assistant.cli gold repin --batch <new-id>   # after a parser revision: move pending rows to the new extraction by element path
python -m rfp_assistant.cli validate-gold --dataset dev-pilot   # approved rows only
```

## Phase 2: corpus coverage and retrieval selection

Plan: [2-corpus-and-retrieval.md](docs/plan/end-to-end/2-corpus-and-retrieval.md). The order below goes from free work to the one paid maintenance step. Angle-bracket values come from the previous command's output.

```powershell
python -m rfp_assistant.cli ingest --profile all             # every unique original once; unchanged inputs are reused
python -m rfp_assistant.cli import-reviews --file C:\abs\reviews.jsonl
python -m rfp_assistant.cli recover-source --doc-id <id> --converted-file C:\abs\converted.pdf --review-file C:\abs\recovery.json
python -m rfp_assistant.cli identity                         # CSV institution/title versus the original's own cues
python -m rfp_assistant.cli resolve-metadata --doc-id <id> --field institution --value '"기관명"' --rationale "..." --evidence "공고문 1쪽" --actor <name>
python -m rfp_assistant.cli build-keyword --reviewed-only                         # structural profile
python -m rfp_assistant.cli build-keyword --reviewed-only --profile fixed-512-64  # comparison profile, never serves users
python -m rfp_assistant.cli evaluate-retrieval --dataset dev-pilot --runs K0,K1   # free lexical comparison
python -m rfp_assistant.cli evaluate-retrieval --dataset dev-pilot --runs K1 --index <fixed index version>
python -m rfp_assistant.cli plan-embeddings --index <keyword index version>       # tokens and maximum spend, no call
python -m rfp_assistant.cli build-dense --index <keyword index version> --estimate-id <id>   # paid, stop the UI first
python -m rfp_assistant.cli evaluate-retrieval --dataset dev-pilot --runs D,H --allow-paid-queries
python -m rfp_assistant.cli trial-reranker --dataset dev-pilot --candidate-counts 10,20
python -m rfp_assistant.cli compare-runs --run-id <K0 run> --run-id <K1 run> --run-id <H run>   # table + K1-default recommendation
python -m rfp_assistant.cli draft-activation --runs <run>,<run>,<run> --out C:\abs\decision.json  # inert until the owner fills it
python -m rfp_assistant.cli activate-run --run-id <run id> --decided-by <name> --note "..."   # note optional; or --decision-file C:\abs\decision.json
python -m rfp_assistant.cli report --phase 2                 # report.md, manifest.json (exit gates, dataset revalidated now) and source-map.json
python -m rfp_assistant.cli check --phase 2 --provider fake  # every automated invariant, temporary state, no key
```

- Ingest. A source is parsed again only when its original, parser revision, native print, OCR cache or HWP converter changed (`--force` overrides). The extraction revision is bound to the actual output: a converter that produces different text under the same parser fingerprint creates a new revision, resets review and leaves the old artifact and elements in place for pinned gold rows and citations. Every result, with timing and automatic diagnostics (`short_output`, `no_tables`, `thin_tail`, `blank_pages`, `replacement_characters`, `parser_warnings`, start/middle/end probes), is written to `.runtime/reports/ingest-*.json`. The diagnostics point at problems; they never mark a source reviewed. One crashing file is recorded as `error` and the run continues (exit code 1).
- Reviews. `import-reviews` takes JSON or JSONL records: `doc_id` (or `source_hash`), `extraction_id` (must be the active revision), `reviewer`, `status`, `locations` (`[{"element_id": ...}]` from that extraction), `checks`, `limitations` (a list, empty when none were found) and, for `reviewed`, `coverage.sections`. One invalid record imports nothing.
- Recovery. For a quarantined original, `recover-source` takes an approved PDF conversion and a review JSON with `reviewer`, `method`, `compared_locations`, `fidelity_passed` and `mapping_limitations`. A failed comparison keeps the quarantine. A passed one becomes a new extraction revision (pages refer to the converted PDF) that stays `unreviewed` until `import-reviews` checks it against its own element IDs. The original and its failure stay recorded. HWPX recovery is refused until a real artifact needs a parser.
- Identity. Byte-identical associations share one extraction and one vector per payload but keep their own metadata and scope. A conflicting field keeps both values visible. `resolve-metadata` records a canonical value with a written rationale; search filters, ranking and displayed fields then use it, and results keep the CSV values in `csv_metadata`. Evaluation families group byte-identical files and related revisions (same notice number).
- Chunk profiles. `structural` (default) and the fixed baselines `fixed-256-32`, `fixed-512-64`, `fixed-800-96` use the same element order and source-span maps. A piece of an oversized element or row is linked to its siblings: packing keeps the condition with its fact or reports `linked_evidence_missing`. The structural chunker version changed in phase 2, so rebuild the keyword index once. The phase-1 index stays on disk for rollback and issued citations.
- Analyzer and query terms (`kiwi-bm25-2`). The analysis copy of indexed and query text collapses letter-spaced headings (`사 업 비` → `사업비`; 2–6 single syllables whose neighbours are not plain Hangul words) and applies a short observed alias list (`지체 상금` → `지체상금`, `부가세` → `부가가치세`, …); stored evidence and offsets keep the source spelling. Inside the selected scope, K1 drops query terms that cannot discriminate there: terms present in more than half of the scope's chunks (non-positive scope-local IDF), and the selected project's own title/institution terms when the question restates that name. Codes, digits and negations are never dropped, nothing is dropped unless a kept term still matches the scope, and the trace lists what was dropped (`scope_redundant_terms:`). Each document's title/institution terms are frozen into the keyword index (`scope-terms.json`, part of its manifest and version), so evaluation and serving reproduce the same behaviour; a later `resolve-metadata` correction applies after the next `build-keyword`. An index built with another analyzer/query policy, or without that snapshot, is refused by `evaluate-retrieval`, `trial-reranker`, run validation and activation with a rebuild instruction; serving still answers from it but marks every result `index_outdated:`. Rebuilding reuses every cached vector and keeps the old index for issued citations. Policy `retrieval-eval-4` retires runs recorded before this rule. The analyzer version changed: rebuild the keyword indexes; the dense matrix for the new index reuses every cached vector (no provider call).
- Runs. K0 is whitespace BM25, K1 Kiwi BM25, D dense only, H is K1 and D fused by RRF (`1/(60+rank)`), and HR is H plus the local reranker. Runs are retrieval only, frozen by dataset hash, the evaluated population (each eligible row's question, scope and pinned evidence, plus skipped rows), index manifest, analyzer, dense version and limits, and stored under `.runtime/runs/<run_id>/` (`config.json`, `traces.jsonl`, `scores.json`, `report.md`). Metrics are graded against source spans: hit@k, recall and complete coverage, nDCG@5, MRR, packed-context coverage, qualifier losses, code checks, wrong-scope candidates, latency and Wilson intervals. A configuration that already has scores is reused (`--force` reruns it). Only independently reviewed dev rows are scored. Skipped rows are listed with their reason.
- Embeddings. `plan-embeddings` counts unique payloads with `cl100k_base`, subtracts verified cache hits and stores an estimate bound to the index, model, dimensions, payload set, rates and batch limits (24 h). `build-dense` refuses a stale or over-envelope estimate. It reserves and settles each bounded batch in the `embedding` envelope and keeps a failure's settled batches cached. It never resends an unknown outcome: while any embedding attempt's billing is unknown (a timeout, or vectors returned without usage), `build-dense` refuses and paid evaluation queries stop until the attempt is reconciled. The matrix is published only after every row is verified. Query vectors are cached by text, model and dimensions. Free retrieval uses only cached ones. A paid answer embeds an uncached query through the gateway, and evaluation does so only with `--allow-paid-queries` (`gold_eval` envelope).
- Reranker. Optional: `pip install -e .[reranker]` (or the pinned `requirements.txt`). Set `reranker_revision` to the model card's commit hash in the `RFP_CONFIG_FILE` JSON. `reranker_precision` (`fp32` default, `fp16` on CUDA) and `reranker_max_length` are measured settings: each is recorded in the HR run and applied when it serves; an HR run that recorded no precision serves fp32. `reranker_max_concurrency` must stay 1: the shared tokenizer is used only under the model lock, because parallel preprocessing crashed on the real GPU. An inference error falls back to H in serving and turns a trial into a blocked bypass artifact. Traces carry `rerank_queue`/`rerank_infer` milliseconds and truncated pairs, and the trial reports queue and inference p95 under load separately. An unpinned, missing or failing model records the bypass. The trial scores the frozen H pool at each depth and measures warm latency alone and under six concurrent users. Retrieval runs with the frozen H run's limits, embedding and analyzer, so only the reranker changes. The measured concurrency bound is part of the HR run and is what serving uses. Its gate (nDCG@5 +0.03, no new critical failure, at most +1 s p95) is reported as a column; it does not block activation, because the person picks from the table. Critical failures are a code check failing, a candidate outside the scope, or a `numeric_qualifier` row (or a row marked `critical`) losing part or all of its evidence from the packed context.
- Selection. `compare-runs` writes `.runtime/runs/comparisons/<key>.md/.json` from recorded scores only: dataset, index/profile, policy, scored denominator, Wilson intervals, nDCG@5, packed completeness, critical/code/scope failures, latency, query cost and whatever would block activation. Runs over different evaluated populations are never compared or recommended against each other, and a run whose population has since changed (a source revision, review or dataset edit) is blocked until rerun. Its recommendation keeps K1 unless another current-policy mode on the same population has no new critical failure and at least +0.03 nDCG@5; the runner-up becomes the phase 4 finalist. `draft-activation` turns that into a decision file with a rationale draft and the remaining blocking checks; `decided_by` and `rationale` stay empty so it cannot activate anything until the owner completes it.
- Gold drafting inputs. `gold excerpts --out <abs new dir>` writes `excerpts.jsonl` (a few numeric/qualifier, late-table, repeated-code and deadline elements per dev-family document; each excerpt is a bounded window around its triggering fact, with the table header or neighbouring sentences, the trigger, raw-text `offsets` and `context_clipped`) and `drafting-context.json` (queue counts, rejections with reasons and inferences, existing question keys). It contains source text: keep it in local inputs unless the owner shares it. `gold status` lists `pending_invalid` candidates that no longer pass the shared checks (for example a converter case whose document has since been recovered); the review screen shows the same errors. `validate-gold` requires the `converter_unavailable` case only while a source is actually quarantined, and reports `required_types` and `operational_cases`.
- Activation. A person picks a row; nothing activates on its own. `activate-run --decided-by <name> [--note ...]`, the 실험 비교 view's activate action, or a decision JSON (`run_id`, `mode`, `decided_by`, optional `rationale` and `finalist_run_id`) go through the same check: the index and matrix verify, the run is current-policy, and a non-OpenAI embedding still matches its pinned revision and prefixes. A reranker gate result is shown, not required. It switches serving in one transaction and keeps the history in `activations`. Runs and gates carry the evaluation policy version (`EVAL_VERSION`). Only runs scored under the current policy can be activated. An HR selection from an older policy keeps hybrid retrieval without the reranker until a current trial passes. Cached corpus and query vectors make that rerun free. Serving reuses the run's evaluated embedding model/dimensions, depths, RRF constant, evidence limits and reranker input length, whatever the process configuration says. Until then the keyword default serves. If the matrix fails verification or a query vector is unavailable, serving falls back to K1 and the trace records why.

## Comparison runner: pipelines run every variant, a person picks

Plan rule ([0-overview.md](docs/plan/end-to-end/0-overview.md)): pipelines run every variant, AI reviewers approve gold rows, and a person chooses from comparison tables and activates. Nothing runs on a schedule, and nothing changes what serves until the person clicks.

```powershell
python -m rfp_assistant.cli compare --matrix lexical      # K0 versus K1
python -m rfp_assistant.cli compare --matrix chunking     # structural and the three fixed profiles, non-serving indexes
python -m rfp_assistant.cli compare --matrix embedding    # 13 models x dense/hybrid; paid rows stop at a priced estimate
python -m rfp_assistant.cli compare --matrix reranker     # 8 local rerankers x whole-list/below-the-BM25-head
python -m rfp_assistant.cli compare --matrix embedding --only Qwen/Qwen3-Embedding-0.6B   # one model's rows
python -m rfp_assistant.cli compare --approve <estimate id> --approved-by <name>        # then rerun the matrix
python -m rfp_assistant.cli compare-cap --usd 0.50 --actor <name> --reason "..."          # Gemini cap, its own ledger
python -m rfp_assistant.cli golden-counts                 # rows of every gold set by status, 50-versus-55 explained
```

- Matrix. A matrix declares its axes (chunk profile, analyzer, embedding model, retrieval, fusion, reranker and mode, evidence units, depth) and its fixed values; `compare.MATRICES` holds the four named ones, and an absolute JSON path runs a custom one. Every row is scored on the same three populations: the development set inside each question's own document, the development set over the whole corpus, and the needle set over the whole corpus. Only the declared axis changes per row: the embedding matrix keeps fusion `keyword_first:60:1.0:6`, 50 candidates and 10 evidence units, and the reranker matrix reranks the serving hybrid's frozen pool.
- Caches. Keyword indexes, corpus and query vectors, reranker scores (`rerank_scores`, keyed by model and pair) and finished rows (`.runtime/compare/cells/`) are reused, so a rerun only does what is missing. Every row with a run is written as an activatable run under `.runtime/runs/`.
- Models. `models.EMBEDDINGS` and `models.RERANKERS` pin each model's revision, licence, dimensions or input length, precision and documented query/document prefixes. Local models run on the GPU and embed every active chunk into their own vector set. They never run on the CPU: without CUDA they refuse to load. On the first load, each process caps its PyTorch allocator at the dedicated VRAM free at that moment, minus 512 MB. Past dedicated memory, the Windows driver would otherwise spill into shared system memory and slow the GPU severalfold. An out-of-memory error halves the batch, and the smaller batch is kept; a model that does not fit at batch 1 becomes a failure row. Causal-LM rerankers compute only the last position's logits, without a KV cache. The table records size, peak VRAM, cold load and truncation. A model that fails to load, runs out of memory or crashes is a row with its reason.
- Paid steps. OpenAI rows reuse cached vectors; an uncached OpenAI or Gemini corpus build stops as a `needs_approval` row with a priced estimate (`.runtime/compare/estimates/`). It runs only after `compare --approve` records the person's approval: OpenAI spending goes through the shared ledger, and Gemini through its own cap (`compare-cap`, refused below committed spend) and ledger (`external_ledger`).
- Output. Each matrix writes `.runtime/compare/tables/<matrix>.json` and `.md`. 검증 → 실험 비교 shows the same tables: sortable by any column, the best value per column and the serving row marked. A row opens its missed questions and the activate action, which calls `activate-run` with the person's name and an optional note. Activation of a non-OpenAI embedding serves its corpus and query vectors from the same local model inside the server process; the earlier `text-embedding-3-large` run stays activatable for rollback.

## Phase 4: evaluation and release

Plan: [4-evaluation-and-release.md](docs/plan/end-to-end/4-evaluation-and-release.md). Procedure: [runbook §10–12](docs/operations/runbook.md#10-phase-4-evaluation-sealed-run-and-release). Outcome on this branch: [release report](docs/operations/release-report.md) and [phase-4 handoff](handoff/phase4/README.md).

```powershell
python -m rfp_assistant.cli validate-gold --dataset dev                       # gold-2 rows; `test` prints IDs/counts only
python -m rfp_assistant.cli freeze-dataset --dataset dev --actor <owner> --reason "<why>"
python -m rfp_assistant.cli evaluate-retrieval --dataset dev --runs K0,K1,D,H  # free; the sealed split is refused
python -m rfp_assistant.cli plan-run --dataset dev --action answer-finalists   # every remaining attempt priced, nothing sent
python -m rfp_assistant.cli run-answers --estimate-id <id> --actor <owner>     # paid, resumable, UI stopped
python -m rfp_assistant.cli export-review --run-id <A-run>                     # blind sheet; import-review applies it
python -m rfp_assistant.cli freeze-release --run-id <run> --answer-run <A-run> --decided-by <owner> --rationale "..."
python -m rfp_assistant.cli plan-run --action sealed --freeze-id <F-id>        # then run-answers: once per test set
python -m rfp_assistant.cli plan-run --action latency --waves 5 --users 6      # then latency-run
python -m rfp_assistant.cli backup --destination D:\abs\new-dir --actor <owner>
python -m rfp_assistant.cli restore-check --backup D:\abs\new-dir\manifest.json   # fresh staging, paid off
python -m rfp_assistant.cli check --phase all --provider fake --save
python -m rfp_assistant.cli release-report --latest                            # read-only; ready / limited / blocked
```

- Gold-2 datasets. `dev` (`.runtime/datasets/dev.jsonl`) and the sealed `test` (`.runtime/sealed/test.jsonl`) share the review queue with the pilot. Labels are evidence groups of alternative source spans (hash, extraction, element, offsets/cells, exact quote), never chunk IDs; required claims are typed (number + unit, date + time, text patterns) with qualifiers and criticality. The validator rejects unreviewed or self-approved rows, missing quotes, cross-split families and paraphrases, unverified negatives and source failures labeled as absence, and labels any set below the 60 + 60 targets `pilot`. Sealed rows are reviewed only through the owner's CLI (`gold show/decide/second-review`).
- Answer evaluation. At most two development finalists (by default the activated run and its recorded finalist). Each answer goes through the service's own answer path with the finalist's retrieval configuration pinned, charged to the `gold_eval` envelope. Resuming never re-sends a finished row or a row whose billing is unknown. Scores keep denominators and Wilson intervals; text claims and unlabelled citations wait for blind human review, and there is no paid judge.
- Sealed run. `freeze-release` records the release candidate (activated run, code/metric/prompt/model/rate/settings hashes, frozen dev/test manifests, the development selection). The sealed set then runs once; any later run is a labeled post-test regression.
- Release report. `release-report` decides `ready` only when every hard check passes and every quality target is met on sealed gold; otherwise `limited` (unverified, unmeasured, missed or pilot-only) or `blocked` (a failed hard check).
- Active retrieval mode. Serving uses whatever `activate-run` last recorded in the owner's `.runtime`; until then it is the keyword default `kiwi_bm25` with no dense or reranker stage. This repository records no activation of its own; `release-report` and `report --phase 2` print the active run.

## Access and paid use

There is no login (owner decision, reaffirmed for phase 3): every visitor gets the three pages 질문하기, 검증 and 데이터셋 만들기 (layout in [DESIGN.md](DESIGN.md)). The name typed in the header's 이름 field (default `owner`, kept in that browser) is recorded on paid requests, review decisions and corrections; it attributes work but does not authenticate anyone. Budget administration (settlement, reconciliation, external adjustments, paid on/off, the audit log) is owner CLI only: `unresolved`, `settle`, `reconcile`, `adjust`, `paid`, `audit`, each with `--actor` and a reason. Anyone who can reach the server can spend the budget, so keep `--host 127.0.0.1` unless everyone on that network may do so (see the [runbook](docs/operations/runbook.md)).

Local verification: `verification.json` lists the major flows for the local verification service. Each flow runs as `python -B tools/verify.py <flow-id>` with the fake provider and ends with one `local-evidence` block. Browser flows need `pip install -e .[verify]`. See runbook §8.

Paid answers run in the background on a bounded executor (6 workers, 12 admitted requests); the page polls read-only status every second and shows the reserved maximum, the settled cost or the unknown pending cost of its own request. A request is persisted under its idempotency key before it runs, so reruns and double clicks never start a second call. An answer renders only while the screen still asks exactly what it asked (same documents, question, mode and date); otherwise it stays in "내 최근 요청" as history and its billing still settles. Two selected documents allow a balanced comparison (each side retrieved with the single-document limits, exactly as the retrieval gate measures it; an answer that drops a side is rejected, and only an inference comparing the sides may cite both); basic information (typed CSV values with unknown/zero/conflict states) and the structured requirement list are free.

Paid generation stays disabled until the owner records the project dates, the prior use, the allowance and the cap, after rechecking current model prices. The current configuration is a dedicated $5 allowance with a $5 hard cap. The live ledger also carries the owner adjustment `external:pr8-pilot-ledger` ($0.448818, what the PR #8 pilot spent on the same account), so its spent total matches the account:

```powershell
python -m rfp_assistant.cli configure-budget --start 2026-09-30 --end 2026-10-28 --prior-use-usd 0 --prior-use-evidence "<where the number came from>" --allowance-usd 5 --cap-usd 5 --confirm-rates --enable-paid
python -m rfp_assistant.cli budget-status
```

Every paid call reserves its maximum cost atomically against the cap before dispatch, pricing every input token at the cache-write rate. It settles once from reported usage (uncached, cached and cache-write tokens) and keeps timeouts as unknown (pending) cost until reconciled. SDK retries are disabled; nothing retries automatically. Only one process may own the paid gateway per data directory.

## Parser licenses

Recorded before any distribution decision:

| Package | Version | License |
| --- | --- | --- |
| pyhwp | 0.1b15 | AGPL-3.0-or-later |
| PyMuPDF | 1.28.2 | AGPL-3.0 or Artifex commercial (dual) |
| kiwipiepy | 0.24.0 | Apache-2.0 |
| rank-bm25 | 0.2.2 | Apache-2.0 |

## Layout

`src/rfp_assistant/` holds one package: `settings`, `contracts`, `store`, `auth`, `ingestion`, `chunking`, `retrieval`, `dense` (embedding cache, matrix, reranker), `models` (the pinned embedding and reranker registry, local GPU runners, the Gemini ledger), `compare` (the axis-matrix runner and its tables), `budget`, `generation` (the only SDK call site), `service` (also the bounded request executor), `api` (the HTTP routes the screens call, one service function each), `cli`, `evaluation` (pilot and gold-2 validation, frozen retrieval runs, source-span metrics), `gold` (the review queue), `answers` (phase-4 answer runs, scoring, blind review, latency sample), `sealed` (release freeze and the single sealed run), `release` (backup, staged restore, release report), `ops` (fake-provider load check and the phase-3 report). `web/` holds the three pages, 질문하기, 검증 and 데이터셋 만들기; budget administration is owner CLI only. Tests are standard `unittest` under `tests/`.
