# RFP assistant (입찰메이트)

Internal assistant for historical Korean RFPs: search projects, select one document, ask a scoped question, receive one metered grounded answer, and open the original evidence. The plan lives in [docs/plan/end-to-end](docs/plan/end-to-end/0-overview.md); this README covers setup and launch for what is implemented (phases 1 and 2).

## Environment

The tested environment is the conda env `rfp-assistant` (Python 3.12, Windows). `requirements.txt` holds the curated direct pins; `requirements-lock.txt` records every installed version from that environment.

```powershell
conda create -n rfp-assistant python=3.12
conda activate rfp-assistant
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

The HWP converter (`hwp5proc` from pyhwp 0.1b15) is installed into the same environment and resolved automatically from `Scripts\hwp5proc.exe`. To use an isolated converter environment instead, set `RFP_HWP_CONVERTER` to the absolute path of its `hwp5proc.exe`. The converter prints a harmless warning when `xmllint` is missing.

## Configuration

Settings resolve from the repository location, never the working directory. Optional overrides must be absolute paths.

| Variable | Default | Purpose |
| --- | --- | --- |
| `RFP_SOURCE_DIR` | `<repo>/원본 데이터` | `data_list.csv` and `files/` |
| `RFP_DATA_DIR` | `<repo>/.runtime` | SQLite ledger, extractions, indexes, datasets, reports |
| `RFP_CONFIG_FILE` | none | JSON with nonsecret `Settings` fields (unknown keys are rejected) |
| `RFP_HWP_CONVERTER` | env `Scripts\hwp5proc.exe` | HWP → XML converter |
| `OPENAI_API_KEY` | none | Process environment, then the repository `.env`, then `.streamlit/secrets.toml`; never printed |

Without `OPENAI_API_KEY` the app still runs: search, filters, evidence browsing and retrieval traces are free; paid generation reports that the provider is unavailable. `provider: "fake"` in the config file never builds a real SDK client.

Answers use `gpt-6-luna` through Chat Completions with strict structured output, `reasoning_effort: low` and at most 2,000 output tokens, reasoning included. Rates are in `settings.DEFAULT_RATES`: standard tier, $0.10 input, $0.01 cached, $0.125 cache write and $0.50 output per 1M tokens. Evidence is capped at 5,000 tokens, far below the 272K long-context tier.

## Commands

Run from any directory with the environment's interpreter:

```powershell
python -m rfp_assistant.cli init --paid-disabled          # schema + allowance row; never resets spending
python -m rfp_assistant.cli manifest                      # all 100 CSV associations, hashes, duplicates, conflicts
python -m rfp_assistant.cli ingest                        # every original; failures are quarantined with a reason
python -m rfp_assistant.cli build-keyword --include-unreviewed   # operating index over every parsed source
python -m rfp_assistant.cli check --phase 1 --provider fake   # automated invariants in temporary state, no key
python -m streamlit run app.py --server.address 127.0.0.1
```

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

Drafted questions wait in a review queue. On the `질문 검토` screen, a reviewer whose sidebar name differs from the drafter either approves each one into the dataset or rejects it with a reason into the rejection wiki. The drafting procedure for agents, including exact file locations, is [.wiki/gold-drafting.md](.wiki/gold-drafting.md).

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
python -m rfp_assistant.cli activate-run --run-id <run id> --decision-file C:\abs\decision.json
python -m rfp_assistant.cli report --phase 2                 # .runtime/releases/phase-2/report.md
python -m rfp_assistant.cli check --phase 2 --provider fake  # every automated invariant, temporary state, no key
```

- **Ingest.** A source is parsed again only when its original, parser revision, native print, OCR cache or HWP converter changed (`--force` overrides). The extraction revision is bound to the actual output: a converter that produces different text under the same parser fingerprint creates a new revision, resets review and leaves the old artifact and elements in place for pinned gold rows and citations. Every result, with timing and automatic diagnostics (`short_output`, `no_tables`, `thin_tail`, `blank_pages`, `replacement_characters`, `parser_warnings`, start/middle/end probes), is written to `.runtime/reports/ingest-*.json`. The diagnostics point at problems; they never mark a source reviewed. One crashing file is recorded as `error` and the run continues (exit code 1).
- **Reviews.** `import-reviews` takes JSON or JSONL records: `doc_id` (or `source_hash`), `extraction_id` (must be the active revision), `reviewer`, `status`, `locations` (`[{"element_id": ...}]` from that extraction), `checks`, `limitations` (a list, empty when none were found) and, for `reviewed`, `coverage.sections`. One invalid record imports nothing.
- **Recovery.** For a quarantined original, `recover-source` takes an approved PDF conversion and a review JSON with `reviewer`, `method`, `compared_locations`, `fidelity_passed` and `mapping_limitations`. A failed comparison keeps the quarantine. A passed one becomes a new extraction revision (pages refer to the converted PDF) that stays `unreviewed` until `import-reviews` checks it against its own element IDs. The original and its failure stay recorded. HWPX recovery is refused until a real artifact needs a parser.
- **Identity.** Byte-identical associations share one extraction and one vector per payload but keep their own metadata and scope. A conflicting field keeps both values visible. `resolve-metadata` records a canonical value with a written rationale; search filters, ranking and displayed fields then use it, and results keep the CSV values in `csv_metadata`. Evaluation families group byte-identical files and related revisions (same notice number).
- **Chunk profiles.** `structural` (default) and the fixed baselines `fixed-256-32`, `fixed-512-64`, `fixed-800-96` use the same element order and source-span maps. A piece of an oversized element or row is linked to its siblings: packing keeps the condition with its fact or reports `linked_evidence_missing`. The structural chunker version changed in phase 2, so rebuild the keyword index once. The phase-1 index stays on disk for rollback and issued citations.
- **Runs.** K0 is whitespace BM25, K1 Kiwi BM25, D dense only, H is K1 and D fused by RRF (`1/(60+rank)`), and HR is H plus the local reranker. Runs are retrieval only, frozen by dataset hash, index manifest, analyzer, dense version and limits, and stored under `.runtime/runs/<run_id>/` (`config.json`, `traces.jsonl`, `scores.json`, `report.md`). Metrics are graded against source spans: hit@k, recall and complete coverage, nDCG@5, MRR, packed-context coverage, qualifier losses, code checks, wrong-scope candidates, latency and Wilson intervals. A configuration that already has scores is reused (`--force` reruns it). Only independently reviewed dev rows are scored. Skipped rows are listed with their reason.
- **Embeddings.** `plan-embeddings` counts unique payloads with `cl100k_base`, subtracts verified cache hits and stores an estimate bound to the index, model, dimensions, payload set, rates and batch limits (24 h). `build-dense` refuses a stale or over-envelope estimate. It reserves and settles each bounded batch in the `embedding` envelope and keeps a failure's settled batches cached. It never resends an unknown outcome: while any embedding attempt's billing is unknown (a timeout, or vectors returned without usage), `build-dense` refuses and paid evaluation queries stop until the attempt is reconciled. The matrix is published only after every row is verified. Query vectors are cached by text, model and dimensions. Free retrieval uses only cached ones. A paid answer embeds an uncached query through the gateway, and evaluation does so only with `--allow-paid-queries` (`gold_eval` envelope).
- **Reranker.** Optional: `pip install -e .[reranker]` (or the pinned `requirements.txt`). Set `reranker_revision` to the model card's commit hash in the `RFP_CONFIG_FILE` JSON. An unpinned, missing or failing model records the bypass. The trial scores the frozen H pool at each depth and measures warm latency alone and under six concurrent users. Retrieval runs with the frozen H run's limits, embedding and analyzer, so only the reranker changes. The measured concurrency bound is part of the HR run and is what serving uses. It passes only with nDCG@5 +0.03, no new critical failure and at most +1 s p95. Critical failures are a code check failing, a candidate outside the scope, or a `numeric_qualifier` row (or a row marked `critical`) losing part or all of its evidence from the packed context.
- **Activation.** The decision JSON names `run_id`, `mode` (the run's mode), `decided_by`, `rationale` and optionally `finalist_run_id` (the other retrieval finalist for phase 4). `activate-run` verifies the index and matrix and requires a passed gate for HR. It switches serving in one transaction and keeps the history in `activations`. Runs and gates carry the evaluation policy version (`EVAL_VERSION`). Only runs scored under the current policy can be activated. An HR selection from an older policy keeps hybrid retrieval without the reranker until a current trial passes. Cached corpus and query vectors make that rerun free. Serving reuses the run's evaluated embedding model/dimensions, depths, RRF constant, evidence limits and reranker input length, whatever the process configuration says. Until then the keyword default serves. If the matrix fails verification or a query vector is unavailable, serving falls back to K1 and the trace records why.

## Access and paid use

There is no login: every visitor gets the consultant, verification and question-review screens. The name in the sidebar (default `owner`) is recorded on paid requests and review decisions; it attributes work but does not authenticate anyone. Anyone who can reach the server can spend the budget, so keep `--server.address 127.0.0.1` unless everyone on that network may do so.

Paid generation stays disabled until the owner records the project dates, the prior use, the allowance and the cap, after rechecking current model prices. The current configuration is a dedicated $5 allowance with a $5 hard cap:

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

`src/rfp_assistant/` holds one package: `settings`, `contracts`, `store`, `auth`, `ingestion`, `chunking`, `retrieval`, `dense` (embedding cache, matrix, reranker), `budget`, `generation` (the only SDK call site), `service`, `ui`, `cli`, `evaluation`. `app.py` launches the consultant and verifier pages. Tests are standard `unittest` under `tests/`.
