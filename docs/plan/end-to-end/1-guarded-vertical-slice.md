# Phase 1 — guarded original-to-answer vertical slice

Goal: a consultant searches a reviewed HWP or PDF, selects its scope, asks about a fact beyond its opening pages, receives one metered answer, and opens the correct original evidence. A verifier inspects the same retrieval trace. All 100 records have a visible manifest state, including the known conversion failures.

Expected window: day 1. This phase builds every layer in its smallest useful form; later phases expand coverage and behavior. This document is an implementation assignment, not a report of completed checks.

## Entry and required reading

Read [overview](0-overview.md), [shared contracts](implementation-contracts.md), [dataset findings](../../rag/project-and-data.md), [preprocessing](../../rag/preprocessing.md), [chunking](../../rag/chunking.md), [prompts](../../rag/prompts.md) and [budget](../../rag/budget.md). Inspect current repository files and pre-existing edits before creating anything. Do not display `.env`, secret configuration or process environment values.

Inputs are `원본 데이터/data_list.csv`, `원본 데이터/files/`, and `docs/rag/evidence/dataset-audit.json`. The audit is a diagnostic reference, not a substitute for reproducing parsing and original checks. No application currently exists in the plan's starting snapshot; reuse any code that has landed since.

In scope: package/install path, validated settings, server identity, source manifest, structural parsing for representative originals, exact scoped BM25, evidence opening, shared budget gateway, one strict answer, two minimal interface entry points, and 24 reviewed development questions. Out of scope: dense embeddings, local reranker downloads, full experiment controls, final reliability claims and production scaling.

## Files to produce

Create the phase-1 modules listed in the [package contract](implementation-contracts.md), root `pyproject.toml`, `app.py`, a dependency lock file generated from the working environment, and root `README.md` with exact setup/launch commands. The converter may use its own environment and requirements file because its old compatibility must not force application dependencies. Record parser licenses, including pyhwp and the chosen PDF parser, before distribution.

Keep meaningful regression checks in `tests/test_ingestion.py`, `tests/test_retrieval.py`, `tests/test_budget.py`, and `tests/test_generation.py`. Use standard `unittest`, temporary directories, real PostgreSQL transactions and a fake provider transport. Tests run on isolated PostgreSQL databases that `tests/fixtures.py` creates per test environment on the server started by `tools/infra/start-postgresql.ps1`, or on the server named by `RFP_POSTGRES_TEST_DSN`; they never touch the application database. Do not add UI snapshot tests or a new test framework solely for this phase.

CLI commands introduced here:

| Command | Required behavior |
| --- | --- |
| `init --paid-disabled` | Initialize schema/settings idempotently; never reset an existing balance |
| `manifest` | Import CSV associations, verify managed source paths/hashes, report all statuses without paid work |
| `ingest --profile smoke` | Parse/review-status import for the representative set below; preserve failures individually |
| `build-keyword --reviewed-only` | Build an immutable structural/Kiwi index for the labeled reviewed subset |
| `check --phase 1 --provider fake` | Run the automated phase gate in temporary state; no network or academy key |
| `validate-gold --dataset dev-pilot` | Validate independent review, quotes, source revisions, types and family assignments |
| `configure-budget` | Owner-only configuration of dates, prior-use evidence, rates/caps and paid-enabled state; require complete inputs |
| `paid-smoke --profile smoke` | Explicit owner action for bounded HWP/PDF answer checks through the ledger; show the total upper estimate first |

Every CLI flag above is a planned contract to implement, not an existing command. Errors produce a nonzero exit code and an actionable reason; partial conversion failures appear in the manifest/report rather than disappearing.

## Ordered implementation

### 1. Establish install, settings and secrets boundaries

1. Inventory tracked/untracked code and preserve unrelated changes. Create the editable `rfp_assistant` package, one settings loader and the common records. Validate absolute source/data paths and configuration once. Acquire the process-owner lock before real gateway startup and reject a second owner; fake checks use isolated paths.
2. Start paid-disabled. Initialize schema without modifying an existing allowance. Create a fake-provider mode that refuses to instantiate a real SDK client, even if the host has a key.
3. Pin compatible installed versions after a smoke import. Keep the converter environment separate; the audit used Python 3.13.9, pyhwp 0.1b15 and PyMuPDF 1.28.0, which are observations rather than a guarantee for every dependency combination. Use the research primary sources when a pin needs verification. Declare PyMuPDF, Kiwi, `rank_bm25`, OpenAI SDK, token counting, NumPy and Streamlit as the core dependencies; add `tzdata` on Windows when required by `ZoneInfo("Asia/Seoul")`. Local reranker libraries remain optional until phase 2.
4. No login (owner decision 2026-09-30): the sidebar name attributes work and the visitor gets every screen. Keep service-level role checks as declarations so narrower in-process principals are still refused.
5. Add missing ignore entries for runtime, environments, models and private settings. Do not overwrite the existing `.gitignore` or `.env`. README documents secret names and setup steps without values.

### 2. Import the complete manifest and typed metadata

1. Parse CSV with `utf-8-sig` and multiline-safe CSV handling. Map all 12 columns explicitly. Resolve filenames beneath the source root and reject traversal, nonexistent files, unexpected extensions and duplicate association identities.
2. Compute stable document IDs and original hashes. Insert all 100 associations. Missing notice/revision values cannot become primary keys. Preserve raw metadata and typed normalization/quality flags.
3. Detect byte-identical hashes before conversion. Record the two known conflicting pairs and any new pairs. Retain distinct institutions/deadlines as provenance conflicts rather than choosing the first CSV row.
4. Expose `pending`, `parsed`, `quarantined` and review status in a free metadata list. An unparsed project is still discoverable, but cannot supply full-document Q&A evidence.
5. Compare manifest counts to the audit: 100 associations, 96 HWP, four PDF. Differences require a recorded input-change explanation; do not force new files to match historical totals.

### 3. Parse a representative vertical-slice set

`--profile smoke` resolves filenames from the manifest by exact stored identity after matching these known titles. If selection is not unique, list candidates and require an explicit managed ID.

| Original | Check it exercises |
| --- | --- |
| 한영대학 tailored educational environment HWP | Cell/merged-header representation; amount and VAT; detailed requirement after overview |
| 국방과학연구소 records-management HWP | Long body, nested table/prose order and detailed requirements |
| 고려대학교 portal/academic system PDF | All 297 pages considered; physical-page citations and late requirements |
| 서울특별시 map platform PDF | Parser warnings and difficult layout remain visible |
| 한국농어촌공사 AFSIS Cambodia HWP | Malformed XML failure and quarantine |
| 대전대학교 MILE HWP | UTF-16 style decoding failure and quarantine |

1. Timebox converter compatibility and representative original inspection to two hours. Invoke converter arguments as a list with `shell=False`, capture stderr into a bounded internal diagnostic, set a timeout, and validate the output independently of return code.
2. Walk well-formed HWP XML in source order. Implement paragraph/table/cell traversal with nested-cell ownership so text is emitted once. Store merged row/column spans, raw text, structural locations and heading/requirement relationships. Do not use a flat `Text` walk or `hwp5txt` as the final index.
3. Walk every PDF page using text blocks and table detection where appropriate. Store one-based physical pages, bounding boxes, original block order and extraction warnings. Do not globally OCR the four text-bearing PDFs. Flag blank/implausible pages for later recovery.
4. Preserve raw evidence and separate NFC search text. Reject replacement-character repair through `errors="ignore"`. Write atomic UTF-8 artifacts keyed by source/parser revision.
5. Inspect start, middle, end, one detailed requirement, a monetary/date condition and representative tables against each supported smoke original. Record reviewer, exact locations and findings. If layout inspection is unavailable, keep the source unreviewed and report the missing verification instead of claiming success.
6. Known converter failures become quarantined with stable reason codes and safe user-facing text. Keep forensic detail in verifier/admin diagnostics. Do not install or automate Hancom conversion without verifying availability and the actual conversion result.

### 4. Build structural chunks and scoped keyword retrieval

1. Group source elements by section/requirement/table row relations. Preserve code, detailed description, headers, units, mandatory language and exceptions. Apply 300–700-token target/800-token hard limit and oversized-prose overlap only as defined in the contract.
2. Every chunk maps back to raw spans/cells. Token count includes headings. Mark summary versus detailed requirement. The exact inventory must be independent of current top-k passage ranking.
3. Implement a single versioned Kiwi analyzer for both indexing and queries. Preserve original compounds/acronyms, digits and exact requirement keys separately. Begin with a small reviewed domain dictionary; no LLM query rewriting.
4. Apply allowed document/version and typed filters before scoring. For selected-document mode, no candidate outside that scope survives. For explicit exact codes, prefer detailed blocks and refuse prefix/suffix collisions such as `SFR-0010`.
5. `search_projects` returns metadata/historical dates, ingestion state and matching snippets without generation. Use SQL parameters and deterministic filter fields; never execute model-generated SQL.
6. Pack up to six evidence units after source-span deduplication and bounded parent/neighbor expansion. Record target/hard evidence budget and excluded facts. HWP citations navigate the extracted section/cells and offer the original; PDF citations resolve physical page/region.

### 5. Implement budget protection before paid dispatch

Implement the contract's atomic reservation/settlement and recovery states first using the fake provider. The ledger must represent prior use through an evidence-backed adjustment, with initialization unable to overwrite it.

1. Store verified model/rate snapshots, actual start/end dates, purpose envelopes, the $20 denominator and $16 operating cap. Unknown rates fail closed. Paid enablement requires an owner-recorded prior-use baseline; absence of billing information keeps paid mode disabled.
2. Count the complete outbound request conservatively, including strict schema and framing. Reserve uncached input plus maximum output; reserve embedding input separately when that stage arrives. Record the count method and margin.
3. Atomically check remainder and insert each attempt while holding the PostgreSQL write mutex (`SELECT … FROM application_mutex … FOR UPDATE`). Persist before dispatch; no database lock spans provider execution. Bound lock waits and fail paid dispatch if the database is unavailable.
4. Create the SDK only in `generation.py`, with hidden retries disabled and a finite timeout. Settle raw reported usage once, including invalid/refused output. No answer-repair call or model escalation happens automatically.
5. Classify confirmed pre-execution failure separately from unknown timeout/cancellation. Recover dispatching rows as unknown on restart; never refund them through a timer. Validate duplicate settlement and adjustment idempotency.
6. Return a budget snapshot with spent, pending, available, percentage, token totals and tracking freshness. Add an immediate snapshot after reservation/settlement and a read-only periodic UI update. A row with no final usage remains visibly pending.

For a finite first slice, use a nonstreaming Chat Completions call. This reduces partial-JSON handling; streaming can be added only after the same settlement rules pass. Phase 3 moves execution to a bounded background worker so budget/status polling remains responsive during a long request.

### 6. Generate and validate one grounded answer

1. `answer` captures principal, scope/version, index, as-of date, prompt/model/rate settings and idempotency key. Return a clarification for unselected ambiguous requirement codes. Return unavailable/insufficient evidence distinctly for unsupported originals or missing retrieved facts.
2. Direct typed metadata questions and complete requirement inventories use deterministic free routes with provenance. General requirement explanations use retrieved evidence. Phase 1 needs only explicitly selected single-document answers; comparisons arrive in phase 3.
3. Serialize instructions separately from question, metadata and evidence. Use the [research prompt](../../rag/prompts.md), strict production schema from the contract, Korean answer language, low temperature where supported, and 800 maximum output tokens. For the verified Chat Completions contract, call `client.chat.completions.create` with messages, strict `response_format` JSON schema, `max_completion_tokens=800` and `stream=False`; recheck these arguments against the pinned SDK before the smoke. Treat document instructions as data.
4. Admit and dispatch only through the gateway. Validate completion/refusal/length state, strict fields, evidence references, scope and quote locations. Construct links from the evidence map. No completed answer is shown until validation finishes.
5. Settle cost independently of answer validity. Store stage trace, raw usage, selected source spans and a bounded internal error. Display recoverable errors and an explicit retry action with a new estimate; default behavior does not retry.

### 7. Connect both minimal interfaces and the reviewed pilot

Consultant entry: visitor name → historical project list/search → explicit selected title/source → question form → domain status and claim citations → excerpt plus original download. Show source-review limitations and a compact spent/pending indicator. Do not expose raw chunk IDs or reranker settings on the work screen.

Verifier entry: choose a pilot row or enter a scoped question → run retrieval-only by default → inspect elements, chunks, filters, BM25 ranks, evidence locations and final token budget. Paid generation is a separate deliberate action with its estimate. Both pages call the shared service, not duplicate retrieval code.

Prepare 24 independent-reviewed development examples from source families assigned to development before question drafting. Include late content, table qualifiers, repeated codes, missing metadata, provenance conflict and converter unavailability. Keep recovery/unavailability operational cases separate from source-absence gold. Quote validation uses the original/extraction revision, not the chosen retriever's returned snippets. Save review identity and dataset hash.

## Verification

Implement `check --phase 1 --provider fake` to run these invariants on a temporary isolated PostgreSQL database and fixtures without a key:

| Case | Pass condition |
| --- | --- |
| Nested HWP table with a merged header and repeated nested text | Each owned text span appears once; header, amount/unit and location survive |
| PDF evidence with physical page distinct from printed label | Viewer target uses the physical page and preserves the label separately |
| UTF-8 output and CSV BOM input | All new artifacts decode strictly with no BOM; multiline CSV fields survive |
| Duplicate source plus conflicting metadata | Two associations survive; shared extraction is reused; conflict is visible |
| Scoped repeated code and `SFR-0010` distractor | Only the selected document's exact detailed code supplies evidence |
| Missing and zero amounts/dates | Unknowns do not satisfy typed filters; zeros retain a review flag |
| Six simultaneous reservations with only one request's remainder | At most the affordable number dispatches; admission/attempt records agree |
| Same idempotency key and duplicate settlement | One paid dispatch and one settled charge; changed input with reused key is rejected |
| Timeout, restart and malformed generated JSON | Unknown cost is retained; settled invalid output remains charged; no automatic second call |
| Narrower in-process principal calling a verifier function, fabricated evidence ID and escaped source markup | Service denies the undeclared role and invalid citations; content cannot execute HTML |

Use a fake cost example with exactly 100 microdollars available and six concurrent 60-microdollar reservations: exactly one is admitted. Also test a known unused reservation release and a successful cheaper settlement. Do not replace concurrency with six sequential calls.

## Commands after implementation

From the repository root, create/install using the pinned dependency definitions, then run:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m rfp_assistant.cli init --paid-disabled
.\.venv\Scripts\python.exe -m rfp_assistant.cli manifest
.\.venv\Scripts\python.exe -m rfp_assistant.cli ingest --profile smoke
.\.venv\Scripts\python.exe -m rfp_assistant.cli build-keyword --reviewed-only
.\.venv\Scripts\python.exe -m rfp_assistant.cli check --phase 1 --provider fake
.\.venv\Scripts\python.exe -m rfp_assistant.cli validate-gold --dataset dev-pilot
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1
```

Repeat settings/store import and CLI `manifest` from a different working directory using the installed interpreter and absolute configuration paths. The data directory must remain identical. These commands must be documented as prospective until their implementing phase runs them.

Create the isolated converter environment from its verified requirements and configure its absolute executable path. Generate the dependency lock from installed, smoke-tested versions using an explicit UTF-8 file write; avoid shell redirection that can introduce a BOM or UTF-16. A fresh install from the lock must reproduce the package/converter imports before handoff.

Owner configuration and paid smoke are deliberate subsequent operations: first inspect the aggregate maximum estimate and verified academy balance, then enable the configured budget and invoke `paid-smoke`. Development/self-check commands never silently become paid when a key exists. Record actual model, estimated versus provider token counts and charges for the HWP and PDF checks.

## Exit and handoff

Exit when automated invariants pass, all 100 associations have statuses, the supported smoke originals have recorded fidelity checks, a real HWP/PDF question beyond the opening content resolves to original evidence, and its reservation/settlement is recorded. Both minimal entry points and a quarantine state must be demonstrated. The 24-row pilot is reviewed and validated; if review or paid access is blocked, mark the milestone incomplete and name that exact missing gate.

Write `.runtime/releases/phase-1/report.md` with installed/converter versions, exact commands, real/fake checks, screenshots or inspected UI evidence, reviewed source coverage, pilot hash, costs and unresolved failures. Record setup/launch in README and preserve source/config IDs for [phase 2](2-corpus-and-retrieval.md).

| Risk | Response |
| --- | --- |
| Converter compatibility exceeds two hours | Preserve working environment where available; quarantine unsupported inputs and continue supported vertical slice with exact limitations |
| Visual inspection tool/native HWP access is missing | Keep unreviewed status; use approved conversion only after comparing output; do not declare fidelity |
| Prior spending or model access cannot be established | Deliver free search and fake-transport verification; real paid completion remains explicitly pending |
| First answer loses a condition despite valid citations | Correct extraction/chunk/context first, add the case to pilot, and avoid upgrading models before locating the failure |
