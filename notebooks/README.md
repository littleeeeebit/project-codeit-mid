# Before/after comparison

Edit production files under `src/rfp_assistant/`, save, then run
`before_after.ipynb`. The notebook shows the current source and compares a fixed
Git commit (A) with a snapshot of the working files, including uncommitted and
untracked additions (B). Each version executes in a fresh process with the same
input bundle and observation code. No production modules are copied into a
second maintained implementation.

## Setup and source refresh

Use the project's Python 3.12 environment. From the repository root:

```powershell
python -m pip install -e ".[notebook]"
python -m ipykernel install --user --name rfp-assistant --display-name "Python (rfp-assistant)"
python tools/notebooks/compare.py watch
```

Leave the last command running while editing. It regenerates the notebook after
source saves, without executing comparisons. Stop it with Ctrl+C. In JupyterHub,
run it in a terminal inside the same checkout. Open `notebooks/before_after.ipynb`,
select the `rfp-assistant` kernel and choose Run All. Its first cell also starts a
watcher for the lifetime of that kernel. The watcher refreshes source excerpts,
including comments in the selected functions, and clears previous execution
outputs when source, input, baseline or runner files change.

An already open notebook may need closing and reopening to show externally
updated cells. Do not save a stale editor buffer over a freshly generated file.
The notebook is generated: edit the `.py` files, `cases.json`, or the notebook
generator's explanatory text. Do not maintain manual edits in the `.ipynb`.
For a one-time refresh, use `python tools/notebooks/compare.py sync`.

## Run and inspect

The first cell runs both versions and presents the conclusion and code diff.
Each following section shows one role's current source, identical inputs,
before/after outputs, checks and elapsed time. Changed output is labeled
changed, not automatically improved. A failed or missing observation stays
visible. Setup failures make the run inconclusive, never successful.

The runner saves `report.html`, `report.json`, the input bundle, evaluator and
both exact source archives under `.runtime/notebook-comparisons/<run-id>/`.
The HTML report can be opened without Jupyter. The report records the full
commit, source and input hashes, Python and installed package versions.
No cached comparison results are reused. Source edits during execution are
reported; rerun to measure the later files.

Run the same comparison without Jupyter:

```powershell
python tools/notebooks/compare.py run
python tools/notebooks/compare.py run --input C:/absolute/path/cases.json
```

The CLI exits with an error for execution failures, regressions, failing checks
on both sides, or missing counterparts. Inspect the saved report for the cause.

## Choose the baseline

`baseline.json` pins a full commit hash. It does not follow HEAD as commits arrive.
Change it deliberately when the team accepts a new reference:

```powershell
python tools/notebooks/compare.py baseline --ref HEAD
```

Both snapshots use the current installed dependencies. A changed dependency
manifest is prominently reported; this runner does not claim to test a package
upgrade against its former environment. Runtime configuration overrides and the
server's activated retrieval settings are not imported into this offline replay.

## Coverage and inputs

`cases.json` holds synthetic regression inputs adapted from existing repository
cases. These are behavioral checks of production functions, not a benchmark of
the real corpus. Add observed failures and their expected evidence to this file;
both versions receive the exact same updated bundle.

| Section | What is actually executed |
| --- | --- |
| Data | HWP XML parsing, element finalization and structural chunking |
| BM25 | Production Kiwi tokenization and BM25 through the retriever |
| Dense | Vector normalization, dimension checks and zero-vector rejection |
| Model configuration | Registered embedding and reranker specifications |
| Parameters | Code defaults, with offline provider and local paths enforced |
| Retriever | Scoped keyword retrieval, evidence packing and fusion of fixed rankings |
| Context | Production messages and token counting after assembly |
| Generation | Validation of recorded answers against supplied evidence |
| Logging | Production secret redaction |

The input bundle contains document XML, preservation phrases, questions with
document scopes and required/forbidden evidence, fixed fusion rankings, vectors,
recorded answers with evidence, and expected log redactions. Keep IDs unique in
each collection. Empty-evidence questions must explicitly set `expect_empty`.
Use development cases; do not expose sealed evaluation questions through this
notebook. Any private inputs and outputs belong under ignored `.runtime/`, not
in a committed notebook output.

PDF/OCR conversion, database behavior, live embedding inference, reranker
inference and newly generated LLM answers are not executed. Network access is
disabled in replay workers, credentials are removed from their environment, and
no production database is configured. Tokenizer/model resources already required
by the project must be installed or cached. A missing dependency is shown as an
execution failure. Use the existing metered evaluation workflow for live model
quality and cost measurements.

Descriptions explain stable stage responsibilities. The generator refreshes code
mechanically; when a stage changes meaning, update its description and checks in
`tools/notebooks/compare.py` and `replay.py`. Generated notebook text and report
labels are Korean for direct reading in Jupyter.
