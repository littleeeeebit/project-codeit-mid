# RFP assistant (입찰메이트)

Internal assistant for historical Korean RFPs: search projects, select one document, ask a scoped question, receive one metered grounded answer, and open the original evidence. The plan lives in [docs/plan/end-to-end](docs/plan/end-to-end/0-overview.md); this README covers setup and launch for what is implemented (phase 1).

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

`src/rfp_assistant/` holds one package: `settings`, `contracts`, `store`, `auth`, `ingestion`, `chunking`, `retrieval`, `budget`, `generation` (the only SDK call site), `service`, `ui`, `cli`, `evaluation`. `app.py` launches the consultant and verifier pages. Tests are standard `unittest` under `tests/`.
