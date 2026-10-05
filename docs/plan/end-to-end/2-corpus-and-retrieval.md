# Phase 2 — corpus coverage and controlled retrieval selection

Goal: extend the phase-1 workflow to accurately declared supported coverage, retain metadata conflicts and failed originals, and let the person choose keyword/dense/hybrid retrieval from reproducible comparison tables ([operating rule](0-overview.md#operating-rule-pipelines-run-reviewers-approve-a-person-picks)). Local reranking reports its gate as a column and keeps a working bypass.

Expected window: day 1–2. Enter after the [phase-1 handoff](1-guarded-vertical-slice.md) exposes working source/evidence contracts, the gateway and reviewed pilot. Continue gold review and interface work against those contracts; do not wait for every retrieval experiment to finish.

## Required reading and inputs

Read [shared contracts](implementation-contracts.md), [preprocessing](../../rag/preprocessing.md), [chunking](../../rag/chunking.md), [retrieval](../../rag/retrieval.md), [reranking](../../rag/reranking.md), [evaluation](../../rag/evaluation.md) and the actual phase-1 report. Inputs are the 100-record manifest, immutable originals/extractions, review records, pilot dataset, active keyword index, configured rates and remaining allowance. Never infer that the historical 98 syntax successes are 98 fidelity passes.

Primary files: extend `ingestion.py`, `chunking.py`, `retrieval.py`, `evaluation.py` and the existing store/service; introduce `dense.py` only for embedding/index/reranker functions. Extend `tests/test_ingestion.py` and `tests/test_retrieval.py`. Preserve the phase-1 index for rollback and already-issued citations.

## Commands and artifacts this phase introduces

| Command/action | Contract |
| --- | --- |
| `ingest --profile all` | Process every unique original hash and update per-association states; one failed file does not abort the manifest |
| `import-reviews --file <absolute-path>` | Validate reviewer/source revision and checked locations, append review records, never turn raw converter success into reviewed coverage |
| `recover-source --doc-id <id> --converted-file <absolute-path> --review-file <absolute-path>` | Register an approved recovery artifact and original mapping; preserve the failed extraction and original hash |
| `build-keyword --profile structural --reviewed-only` | Publish the reviewed structural baseline |
| `plan-embeddings --index <version>` | Count unique exact payload tokens, cache hits and maximum remaining spend; no provider call |
| `build-dense --index <version> --estimate-id <id>` | Owner-controlled, metered maintenance job; refuse a stale estimate/config/input hash |
| `evaluate-retrieval --dataset dev-pilot --runs K0,K1,D,H` | Freeze snapshots, reuse query vectors, write rank/evidence metrics without answer generation |
| `trial-reranker --dataset dev-pilot --candidate-counts 10,20` | Local load/inference only, frozen H candidates, time/memory and gate report |
| `compare --matrix <name>` | Run every variant of a declared axis matrix over the development, needle and whole-corpus sets, reusing cached indexes, vectors and scores; write one comparison table (JSON and Markdown). A paid step runs only under an estimate the person approved |
| `activate-run --run-id <id> --decision-file <absolute-path>` | The person's pick from a table: validate ready artifacts and atomically change the serving config |
| `check --phase 2 --provider fake` | Incremental-index, ranking, scope and budget regression gate |

Angle-bracket values are supplied from real manifests/review files during implementation, not shell-ready literal arguments. Runtime indexes/reports follow the common layout. Retain old versions referenced by gold, citations or active requests.

## Ordered work

### 1. Expand source extraction and review

1. Process all unique source hashes with the same structural parser revision. Reuse existing successful artifacts with verified manifests; record conversion time, warnings, source-order counts and failure details. Per-document exception handling must preserve progress without swallowing errors.
2. Add checks for tables/cells, detailed IDs, total pages/sections, start/middle/end content and unexpectedly short/blank outputs. These diagnose problems; none grants a fidelity pass automatically.
3. Queue original inspection by risk: amount/deadline/submission tables, merged headers, long requirement blocks, nested/layout tables, PDF page transitions, known warnings and late sections. Each claimed supported document gets recorded review coverage and limitations. Run broader automated content checks across all extracted elements.
4. Do not make a global `reviewed=True` because one table worked. Report the exact documents/sections inspected and outstanding table risks; disable unsupported exhaustive claims. Require checked critical evidence before critical-field answers rely on it.
5. Handle AFSIS Cambodia and MILE separately. Verify approved Hancom/native conversion availability, convert to PDF or HWPX, compare original/output, and register artifact hashes and mapping. If unavailable or fidelity fails, keep quarantine. Do not write an HWPX parser solely for nonexistent corpus files; implement it only if a real recovery artifact needs it.
6. OCR only demonstrated problematic PDF pages using local Korean/English language data. Cache by original/page/config hash and inspect numeric/code errors. Docling remains a measured alternative for difficult PDF pages, not a blanket new HWP parser.
7. Keep immutable originals; attach recovered output as another extraction revision. Gold/trace references to older revisions require migration/review or remain pinned. Never silently resolve an old evidence ID against a different conversion.

### 2. Resolve metadata identity without discarding conflicts

Use the two audited duplicate pairs as mandatory cases. Extract institution/title cues from originals, compare them with CSV records and retain both provenance values. Notice identity, contracting institution, source website label and source filename are different concepts.

For conflicting deadline/amount/institution, show each value and source. Owner/reviewer canonicalization requires a written source/notice rationale and timestamp; CSV order is not authority. Maintain association-specific filtering and display while reusing source content embeddings. Group byte-identical and related revisions into evaluation families before question splits.

### 3. Freeze structural and fixed chunking comparisons

1. Keep extraction revisions and source-span labels constant. Structural units preserve requirement detail/summary distinctions, table header-value relations, exceptions and annexes.
2. For oversized prose/cells, split with token-aware raw-span mappings and repeat the necessary heading/header context. Every resulting search payload obeys its configured token ceiling. If a condition cannot fit with its fact, record linked evidence and pack them together later; do not drop it silently.
3. Implement fixed windows `256/32`, `512/64`, `800/96` as separate versioned profiles using the same element order and source maps. Do not merge across originals or revisions.
4. The chunking matrix builds every profile as a non-serving index and scores it with K1 on one population: chunk count, duplicated tokens, nDCG@5, complete support, qualifier losses, critical failures and latency. Embedding a profile costs money only for API models, and only under an estimate the person approved.
5. Persist config/hash and row mappings for each ready index. Validate every mapped raw span/cell; same source evidence repeated by overlap counts once in metrics and context.

### 4. Make the lexical baseline reproducible

K0 uses a documented whitespace analyzer; K1 uses the exact Kiwi analyzer/dictionary shared by ingestion/query. Keep punctuation-bearing codes in a separate exact lookup. Preserve meaningful negative/mandatory expressions; stopword changes require a recorded development case.

Apply scope/version constraints to admitted candidate rows before ranking. Declare whether BM25 IDF is global or recomputed within a scope and hold that choice fixed across comparisons; global IDF with scope-masked candidates is a reasonable initial implementation. Zero matching tokens returns an empty lexical result rather than arbitrary zero-score chunks.

Code requests use exact detailed inventory targets before BM25, with boundaries that reject suffix collisions. Unknown/ambiguous aliases yield explicit clarification. Make tie order deterministic by stable chunk ID. Title/institution metadata search remains a separate free discovery route rather than repeated heading weighting.

### 5. Build dense artifacts once through the gateway

1. Define payload hash from the exact normalized text sent, embedding model, requested/returned dimensions and normalization policy. Source bytes reused across associations share payloads. A metadata prefix that differs across conflicting CSV records must not accidentally create different supposedly deduplicated embeddings.
2. `plan-embeddings` tokenizes final unique chunk payloads, subtracts verified cache hits and estimates the maximum batch spend against category/global remainder. Character counts are not tokens. Record corpus/index/model/config fingerprint and estimate expiry/invalidation rules.
3. Paid build requires an explicit matching estimate and owner maintenance mode. Check current endpoint per-input/batch limits from the pinned official documentation. Use bounded batches; reserve each attempt before sending and settle reported input tokens. Partial progress survives failure without repeating settled batches.
4. Cache each successful vector with payload/model/dimension hash and integrity metadata. Store vector arrays as safe NumPy data with `allow_pickle=False`. Unknown billed outcomes do not trigger an automatic resend; they need recovery/reconciliation.
5. Normalize nonzero vectors to unit length. Reject empty, nonfinite or inconsistent-dimension vectors. Persist matrix row-to-chunk mapping and content hashes; verify count/order/dimensions before marking ready.
6. Publish a complete immutable manifest and active pointer atomically only after all required vectors are verified. A half-built dense matrix is never silently used. The existing keyword index remains available throughout.
7. Cache query embeddings by normalized query/model/dimensions. Cache misses pass through the gateway. Answer-cache keys also include authorized scope, as-of date, index/model/prompt/config versions; no semantic answer cache.

If the embedding estimate exceeds its envelope, stop before spending and reduce candidate chunk profiles or stay with K1. Do not secretly take money from gold/interactive allocation; explicit owner reallocation retains the global cap.

### 6. Implement dense, fusion and bounded packing

D uses cosine dot products over the normalized matrix, restricted to authorized document/version rows. If no rows match, return empty before creating a paid query vector. Stable tie-breaking and score/rank records are required. Matrix or manifest mismatch fails to the known keyword mode with a trace reason; no on-demand corpus rebuild during a consultant question.

H retrieves K1 top 20 and D top 20, fuses by unique chunk ID with `sum(1/(60+rank))`, takes 20 candidates, and preserves per-channel ranks. Duplicate associations or overlapping spans do not earn extra votes. Exact identifier and selected-scope rules survive fusion. Do not average BM25 and cosine scores.

Pack at most six evidence units after expansion/deduplication, with target 3,000/hard 5,000 evidence tokens. Store the pre-pack rank list separately from packed context so recall loss can be located. Reject/truncate source-aware units at boundaries; never cut a date/amount qualifier to hit a token count. Full prompt counting and reservation follow packing.

### 7. Trial local reranking without blocking delivery

1. Read the official candidate card, verify applicable license and pin model revision/files. Record download size, RAM/VRAM, Python/library versions and CPU/GPU choice. Optional reranker dependencies are separate from the base installation.
2. Load `BAAI/bge-reranker-v2-m3` once. It is distinct from `BAAI/bge-m3`. Measure cold load and warm query-passage scoring with the model's tokenizer; OpenAI token counts do not define its input length.
3. Score the same frozen H candidate pool at depths 10 and 20 before parent expansion. Protect scoped exact detail matches. Deduplicate source overlap, preserve headers and report any tokenizer truncation/windowing; never assume a sigmoid score is a confidence percentage.
4. Bound concurrent inference and measure queue time under six users. If CPU loading, memory, OOM or latency is unacceptable, record the failure and bypass. No paid LLM reranking fallback.
5. Report the gate as a column: dev nDCG@5 gain of at least 0.03, no new critical code/scope/numeric regression, added warm p95 of at most one second under six-user load. The person reads it next to the other rows and decides; pilot uncertainty stays visible in the Wilson and bootstrap intervals.

### 8. Select and record the active mode

Evaluate K0, K1, D, H and eligible HR on identical source/scope/query versions. First comparisons are retrieval-only; only query embeddings are paid on uncached dense runs. Cache them across runs and report that cost distinctly. Record pre-rerank recall, post-ranking metrics, packed-context evidence coverage, wrong-document/version cases and latency.

The runner places development evidence, critical regression checks, runtime and total cost side by side; the person picks a row and activates it. Until then the current activation (or K1) keeps serving. Freeze the selected run/config and keep another retrieval finalist for phase 4 answer comparison. Update consultant-visible mode/limitations without exposing internal scores.

## Verification matrix

| Case | Expected result |
| --- | --- |
| Re-run unchanged corpus | No repeated conversion or chunk embedding calls; hashes and active artifacts remain stable |
| Modify one source in a temporary fixture | Only changed source/profile payloads rebuild; old citations remain pinned |
| Duplicate bytes under conflicting metadata | One content vector per exact payload; separate association identity and scope retained |
| Corrupt matrix, dimension or row manifest | Refuse ready/active status and use recorded keyword fallback |
| Korean decomposition, compound/acronym and exact-code distractors | Same analyzer at query/index time; documented domain cases retrieve correct detail |
| Table header and VAT/exception split | Packed evidence retains the full condition or reports missing coverage |
| Known RRF lists with overlapping IDs | Formula and stable ordering match hand-calculated ranks; absent channel contributes zero |
| Zero lexical hits or empty scope | No arbitrary zero-score evidence; empty scope incurs no query embedding call |
| Failed/budget-blocked embedding batch | Partial cache remains valid; no incomplete index promotion or hidden retry |
| Reranker unavailable or scope-conflicting candidate | Bypass or reject safely; correct scope cannot leak |

Run the phase-1 gate again only where changed code touches its invariants, then the new phase-2 gate. Run retrieval comparisons once per frozen configuration; repeat when a failure or actual change warrants it. Use held-out synthetic ranking fixtures for deterministic algorithms, not tests that merely duplicate the implementation.

## Exit and handoff

The release candidate has a 100-record manifest, reproducible source/index artifacts, documented reviewed coverage and recoveries, scoped exact/keyword search, working fallback, and frozen retrieval comparison results. If dense/reranker is not activated, its missing gate and the chosen baseline are explicit. The shared ledger reports indexing/query costs and leftover envelopes.

Write `.runtime/releases/phase-2/report.md` with source review manifest, quarantine/recovery list, family map, chunk/index versions, K0/K1/D/H/HR results where run, model load/latency numbers, actual spend and the selected/fallback decision. Pass active index/config and immutable evidence mappings to [phase 3](3-workflows-and-operations.md), and frozen retrieval finalists to [phase 4](4-evaluation-and-release.md).
