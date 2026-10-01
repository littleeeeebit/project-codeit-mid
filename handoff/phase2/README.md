# Phase 2 local execution and Cloud implementation handoff

This kit supplies the files and commands requested in [Cloud's PR #4 handoff comment](https://github.com/littleeeeebit/project-codeit-mid/pull/4#issuecomment-5922582946). Start with `baseline/manifest.json`: it is a read-only inventory of the current local runtime, not a completed Phase 2 run. The implementation base is PR #4 commit `032a09d2fcf045913206a2909b26befac5caa169`; that PR was still open when this kit was prepared.

Originals and the authoritative `.runtime/` remain local (D1=A). The kit needs no new dependencies. Everything written by its tools is UTF-8 without BOM. The [Phase 2 plan](../../docs/plan/end-to-end/2-corpus-and-retrieval.md) and [contracts](../../docs/plan/end-to-end/implementation-contracts.md) remain authoritative.

## Files and their meaning

| File | Use |
| --- | --- |
| `baseline/manifest.json` | Actual code revision, host/package versions, dataset hash, active selection and pending work |
| `baseline/manifest-report.json` | Document/source IDs, current extraction IDs, parse/review states, navigation anchors and index inventory |
| `baseline/review-coverage.json` | Current review state and whether the latest recorded human review belongs to the active extraction |
| `baseline/fidelity-summary.json` | Recorded automated verdicts, finding counts and positional references; no source body |
| `baseline/budget-status.json` | Existing configuration, attempts by purpose/stage/state, adjustments and unresolved attempt IDs |
| `baseline/hardware.json` | Actual CPU, RAM bytes, GPU name/VRAM/driver observation; no inference timing claim |
| `baseline/validation-applicability.json` | Read-only reproduction of the operational gold-type mismatch; the synthetic diagnostic row was never imported |
| `cloud-next.md` | First coding task, minimal change location and offline acceptance matrix for recovered-corpus gold validation |
| `templates/*.example.json` | Deliberately incomplete input shapes; copy and complete locally before use |
| `.private/` | Ignored command output and timing/exit records |
| `local-inputs/` | Ignored human-completed reviews, recovery files and decisions |
| `results/<timestamp>/runs/<run_id>/` | Selected comparison artifacts exported later with original body fields omitted |

No `reviews.jsonl`, successful comparison, release report or approval is fabricated. Missing evidence stays missing. Automated fidelity verdicts are not human approval. Navigation anchors are merely IDs for opening a document, not inspected locations.

## Local operator procedure

Run from the repository root using the existing `rfp-assistant` Python environment. An isolated worktree defaults to its own empty runtime: explicitly point both paths at the authoritative original checkout before calling application commands.

```powershell
$env:RFP_DATA_DIR = 'C:\Users\dasdk\PycharmProjects\project-codeit-mid\.runtime'
$env:RFP_SOURCE_DIR = 'C:\Users\dasdk\PycharmProjects\project-codeit-mid\원본 데이터'
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
```

1. After PR #4 lands, use its merged code for actual Phase 2 execution. Retain the existing environment rather than reinstalling GPU packages just to collect inventory. Stop paid UI/CLI activity before any authorized paid maintenance job.
2. Copy `templates/decisions.example.json` to `local-inputs/decisions.json`. D2 already has owner-approved local configuration; retain it unless the owner changes it. Check the actual budget snapshot rather than re-entering `$0` prior use. D3/D4 stay pending until the owner decides. If dense is approved, record both the matching build estimate and a separate maximum for paid evaluation query misses.
3. Capture each command separately. The wrapper runs exactly one requested application command, writes all output privately, records its real exit code, and throws on failure. It does not schedule further steps. Logs can contain source text: inspect and redact before sharing. `steps.jsonl` records command failures as well as successes.

```powershell
./tools/capture_phase2.ps1 -Name phase2-check -CliArgs @('check','--phase','2','--provider','fake')
./tools/capture_phase2.ps1 -Name manifest -CliArgs @('manifest')
./tools/capture_phase2.ps1 -Name ingest -CliArgs @('ingest','--profile','all')
./tools/capture_phase2.ps1 -Name fidelity -CliArgs @('fidelity','run')
./tools/capture_phase2.ps1 -Name identity -CliArgs @('identity','--all')
```

4. Independently inspect originals and flagged fidelity locations. Read current IDs from a fresh inventory after ingest. Copy `review.example.json`, supply the actual reviewer, active extraction and inspected element IDs, and record `amount_vat`, `deadline`, `merged_header` and `tail` checks where applicable. Choose `sample_checked` for limited inspection; `reviewed` additionally requires actual `coverage.sections`. Finding a mismatch should lead to `needs_recovery` and a failure record, rather than a positive review containing an unresolved critical mismatch. Build a real JSONL file from completed records and import with an absolute path.

```powershell
./tools/capture_phase2.ps1 -Name import-reviews -CliArgs @('import-reviews','--file',(Join-Path (Get-Location) 'handoff/phase2/local-inputs/reviews.jsonl'))
```

5. AFSIS and MILE are already parsed through native-print recovery in the local Phase 1 state. They remain unreviewed because that print cannot also serve as an independent witness. Inspect their originals and current output; do not call `recover-source` merely because the old planning text described quarantine. That command accepts only a currently quarantined source and an independently compared PDF, using the completed recovery template. Keep any unresolved fidelity limitation explicit.
6. An independent person reviews the gold queue in the existing UI. The baseline has 23 pending, 1 rejected and 0 approved candidates. Re-pinning may be required if ingest creates new extraction IDs; verify quotes again. Approval of all 23 pending rows still leaves fewer than 24 approved rows. Read [Cloud's first coding task](cloud-next.md): the validator currently mandates an operational type that is impossible for this entirely parsed corpus. Read the actual rejection and infer its drafting lesson before submitting additional rows. Draft and independently review additional difficult rows using the gold template, including numeric qualifiers, late table facts and repetitive codes, then validate `dev-pilot` after the validator repair.

```powershell
./tools/capture_phase2.ps1 -Name validate-dev -CliArgs @('validate-gold','--dataset','dev-pilot')
./tools/capture_phase2.ps1 -Name build-structural -CliArgs @('build-keyword','--reviewed-only','--profile','structural','--no-activate')
./tools/capture_phase2.ps1 -Name build-fixed -CliArgs @('build-keyword','--reviewed-only','--profile','fixed-512-64','--no-activate')
```

7. Record the actual index versions printed by those builds. Supply them explicitly: building a comparison index must not change the serving pointer. Replace the variables below with those real versions before executing.

```powershell
$structuralIndex = '<actual structural index version>'
$fixedIndex = '<actual fixed-512-64 index version>'
./tools/capture_phase2.ps1 -Name lexical-structural -CliArgs @('evaluate-retrieval','--dataset','dev-pilot','--runs','K0,K1','--index',$structuralIndex)
./tools/capture_phase2.ps1 -Name lexical-fixed -CliArgs @('evaluate-retrieval','--dataset','dev-pilot','--runs','K1','--index',$fixedIndex)
./tools/capture_phase2.ps1 -Name embedding-plan -CliArgs @('plan-embeddings','--index',$structuralIndex)
```

8. Share the real cost estimate for an owner decision. If approved, run `build-dense --index <version> --estimate-id <id>` with that matching estimate and then bounded `evaluate-retrieval --dataset dev-pilot --runs D,H --index <version> --allow-paid-queries`. These commands are intentionally not run by this kit. Stop paid work on any `unknown` billing attempt; preserve its ID and obtain provider evidence before reconciliation or retry. Do not infer provider usage from a zero settled amount.
9. If HR is approved, supply the pinned reranker revision through `RFP_CONFIG_FILE`, retain other needed configuration fields, install the existing optional reranker extra only as needed, and run `trial-reranker --dataset dev-pilot --candidate-counts 10,20 --index <version>`. Record device, driver, VRAM, actual dependency versions, six-user p95 timings and gate result. Do not substitute synthetic timings or a bypass for a measured pass.
10. Capture `budget-status` before and after paid work. Generate `report --phase 2` from the real state. Cloud drafts `activation.example.json` using an actual complete current-policy run; its `mode` must match that run (`kiwi_bm25`, `dense`, `hybrid` or `hybrid_rerank`). The owner reviews the selection and performs activation separately. No activation is part of collection.

## Collect and share the next snapshot

The collector uses standard-library SQLite in `mode=ro`, starts a read transaction, and never calls application initialization. It refuses an existing output directory so earlier snapshots survive. Both paths must be outside each other. Supply `--run-id` once for every real retrieval run Cloud should analyze; default collection exports no runs.

```powershell
python tools/phase2_handoff.py --runtime $env:RFP_DATA_DIR --out handoff/phase2/results/2026-10-01-post-lexical --run-id '<actual-run-id>'
```

Review the resulting files before committing. Questions remain in selected traces, while payloads, raw elements, prompts, quotes and unknown top-level trace fields are omitted. Recognizable API keys are redacted, but free-form failure descriptions and questions still need human inspection. Never add `.env`, the database, originals, `elements.jsonl`, `.npy`, private logs or completed local inputs wholesale.

Add manually inspected companions only when available: redacted `commands.log` and its `steps.jsonl`, the actual dataset-validation report, imported review records, identity/metadata decisions, sanitized ingest failures, and the real Phase 2 report. Ingest errors should retain exception class, stack-frame function names and IDs; remove secrets, local paths and source excerpts from exception messages. Explain omitted details. The collector intentionally does not copy raw tracebacks or arbitrary Markdown.

Mark whether each artifact is baseline inventory, a real operational run, or a synthetic test. Keep failed and skipped commands in the ledger. State an explicit reason for every missing gate. A single budget snapshot is not a before/after measurement, and accumulated attempt totals are not this session's spend.

## Cloud's next coding tasks

1. Read the baseline facts and completed operator decisions. Keep compatibility with PR #4's current-policy evaluation identity and frozen serving settings. Preserve source revision IDs and the shared ledger.
2. Convert each actual failure record into one reproducible test with fake vectors or a local fixture before changing production code. State the trigger, expected behavior and affected IDs. If source excerpts are indispensable, request a narrowly scoped authorized fixture instead of requesting the entire corpus or ledger.
3. Interpret real comparison results in a table with dataset hash, index version, profile, mode, scored denominator, Wilson interval, nDCG@5, packed completeness, critical failures, scope/code failures, latency and cost. Use actual `scores.json` values; missing measurements stay null. Compare H and HR only with the same frozen H candidate pool/configuration.
4. Draft the selection decision and Phase 2-to-3 handoff using real run IDs. Include supported coverage/limitations, active and rollback artifacts, source mappings, optional mode gaps and the Phase 4 finalist. Leave owner identity/rationale blank until supplied; the example is deliberately unusable as an approval.

Acceptance: Cloud can load every shared JSON/JSONL file offline, locate a reported failure by IDs, distinguish unrun work from success, and propose a regression/fix or a selection rationale without secrets, originals, a paid dispatch or guessed measurements.
