# Phase 4 handoff: evaluation and release

This folder records what the Cloud implementation of the [phase 4 plan](../../docs/plan/end-to-end/4-evaluation-and-release.md) built and checked. All evidence here is **synthetic**: the fake provider, the phase-4 fixture corpus (five CSV associations: four generated PDFs, one unconvertible HWP) and typed reviewer names. No original document, no local runtime and no API key were used, no paid call was made, and **$0 was spent**. No reviewed gold exists in this repository, so no real quality number is claimed anywhere below.

The authoritative release evidence must be produced on the owner host with the real corpus, following [runbook §9–12](../../docs/operations/runbook.md#9-setup-and-the-api-key). The shareable outcome is [docs/operations/release-report.md](../../docs/operations/release-report.md).

## What changed

| Area | Files | Summary |
| --- | --- | --- |
| Gold schema | `evaluation.py` (`GoldChecker`, `validate_gold_v2`, `freeze_dataset`) | `gold-2` rows for `dev` (`.runtime/datasets/dev.jsonl`) and the sealed `test` (`.runtime/sealed/test.jsonl`): stable `question_id` + `revision`, scope of one or two documents, families, typed required claims (number + unit, date + time, text patterns; qualifiers; criticality), evidence groups of alternative source spans (hash, extraction, element, offsets/cells, exact quote), negative validation, review and generation provenance. The validator refuses unreviewed/self-approved rows (including a drafting model), missing quotes, wrong offsets/cells, chunk-only labels, cross-split families, the same original in dev and test families, repeated or paraphrased questions across splits, unverified negatives, source failures labeled as absence, operational cases and unresolved disputes. It reports per-type counts against the 60 + 60 targets, source positions and rejected rows, labels anything short of the targets `pilot`, and never prints sealed question text or a sealed ID. `freeze-dataset` records the dataset, review-log and family-map hashes. |
| Review queue | `gold.py`, `service.py`, `ui.py` | The same queue takes gold-2 rows. Corrections are new revisions. Approval requires the "original inspected" attestation and can mark a row disputed; a third person records the second review. Sealed candidates, batches and rejection pages live under `.runtime/sealed/` and are reviewed only through the owner's CLI (`gold show/decide/second-review`). Schema 6 adds the append-only `gold_reviews` log and `eval_estimates`. |
| Metrics | `evaluation.py` | Evidence groups with alternatives (one fact counts once), deduplicated overlapping chunks, two-document rows scored per document (recall and complete coverage; hit/nDCG/MRR not applicable), gold-2 nDCG@5 = `(2^g−1)/log2(r+1)` against one complete span per group, empty denominators reported as not applicable, family-grouped bootstrap intervals (seed 20261001, 1000 resamples). Every run records the Git revision (or that none exists), whether tracked files differed, package-source and metric-code hashes and hardware. Pilot (`dev-pilot`) scoring is unchanged, so `EVAL_VERSION` stays `retrieval-eval-4`. |
| Answer runs | `answers.py`, `service.py` (`paid_purpose`) | `plan-run` prices every remaining answer exactly as the answer path will, without a call, and binds the estimate to the finalists, population, prompt, model, output cap, rates and per-row token counts. `run-answers` runs at most two finalists through the service's own answer path with each finalist's retrieval configuration pinned, charged to `gold_eval`. It stops at the first budget refusal or new unknown billing; resuming never re-sends a finished row or an unknown-billing row, and runs a settled/reconciled row once more as a recorded attempt. Scoring: typed claims matched per document, numbers only with their unit and deadlines only with their time (`correct`, `wrong_value`, `contested`, `incomplete_qualifier`, `missing`, `needs_review`), critical wrong values, citation support from source spans (otherwise unjudged), precision over judged links and as a lower bound, link validity through the service's evidence view, negatives, unnecessary refusals, metadata stratum, scope leaks, cost and latency, plus retrieval metrics of what each answer actually served. `export-review`/`import-review` give a blind, shuffled review sheet; there is no paid judge. |
| Sealed run | `sealed.py` | `freeze-release` (owner, `sealed_evaluator`) refuses unless the activated run is the servable selection, both splits are validated, frozen and unchanged, and a complete development answer run used it. The sealed set then runs once; a changed code/prompt/model/rate/settings/activation/dataset breaks the freeze; a later run is a labeled post-test regression with a reason. Audit events record the freeze and each sealed start. |
| Release | `release.py`, `cli.py` | `backup` (online SQLite backup, mutable trees copied, immutable artifacts hashed), `restore-check` (fresh staging, paid off, fake provider; ledger amounts, prior use, attempts, files, extractions, active index and dense rows verified; restart-recovery preview), `release-report` (read-only; manifest, coverage, evaluation, budget; `ready`/`limited`/`blocked` with reasons), `check --phase 4|5|all --save`, `plan-run --action latency` + `latency-run` (waves of concurrent answers; cold and warm separately). |
| Verifier UI | `ui.py`, `DESIGN.md` | 검증 › 평가 (4단계): development gold status and targets, the sealed set as a count, answer runs with denominators, a free estimate and a consented, estimate-bound run on the application's gateway (one at a time, stopped by a controlled stop), the release decision. 질문 검토 renders gold-2 rows (claims table, every evidence alternative, negatives) with the attestation form and the second-review list. |
| Verification | `verification.json`, `tools/verify.py`, `tools/verification/phase4_walkthrough.py` | New `evaluation-release` flow (unit tests plus a 31-step CLI walkthrough on a temporary fixture corpus) and a `phase4-gate` assertion in `repository-gates`. |
| Tests | `tests/test_evaluation.py`, `tests/test_release.py`, `tests/phase4_fixtures.py` | Every row of the plan's metric/check regression fixture table, gold queue and sealed routing, retrieval and answer runs, interruption and budget-refusal resume, blind review, freeze and the single sealed run, backup/restore, the phase-5 reconciliation fixture, the release report and the two new screens (Streamlit AppTest). |

## Commands run here and their outcomes

| Command | Outcome |
| --- | --- |
| `check --phase 4 --provider fake --save` | Passed: 50 tests. See `checks/check-4.json`. |
| `check --phase all --provider fake --save` | Passed: 238 tests (the 199 earlier ones plus 39 new). See `checks/check-all.json`. |
| `python -B tools/verification/phase4_walkthrough.py <work>` | Passed: 31 of 31 CLI steps, including the refused approval without inspection, the refused sealed `evaluate-retrieval`, a finalist run, a replan with 0 remaining rows, the release freeze, one sealed run and its refused repeat, a fake latency sample, backup and a passing restore check. See `synthetic/phase4-walkthrough.json`. |
| `release-report --latest` (on that synthetic runtime) | `limited`: the saved full check was not part of that runtime, multi-evidence coverage and negative handling had no sealed denominator, there was no real latency sample, and the sealed set is a 1-row pilot. See `synthetic/release-report.md`. |
| Browser (headless Chromium, Streamlit on the synthetic runtime) | 평가 tab, estimate, gold-2 review, refused approval without the attestation and a 390 px layout without horizontal scroll. See `screenshots/20–24`. Keyboard, contrast and screen-reader checks were not repeated for these additions. |

## Decisions made during implementation (for owner review)

1. Two nDCG formulas, by schema. Gold-2 rows use the plan's graded formula; `dev-pilot` keeps the phase-2 increment formula so frozen `retrieval-eval-4` runs stay comparable. Reports name the dataset, and runs over different populations are never compared.
2. Two-document questions have no single ranking. They count toward evidence recall and complete coverage, scored per selected document, and are excluded from hit@k, nDCG@5 and MRR denominators.
3. Citation support is deterministic only when the generated claim states the labelled value. A link supports only if the cited chunk holds a complete gold span and every sentence of that claim states the typed value the span establishes (review round 1). Everything else stays unjudged until a blind reviewer decides. The release gate uses the lower bound (unjudged counted as unsupported).
4. No paid judge. The plan allows a calibrated, sampled LLM judge; none is implemented or planned, so judge cost is always 0. Blind human review covers text claims and unjudged citations.
5. Sealed rows never reach a screen. Their review, freeze and run are owner CLI actions; the 평가 tab shows only the sealed row count and freeze state, and the dev validation report withholds sealed IDs.
6. Evaluation identity. Answer-run requests use the member `evaluation-job` (so a resume under another typed name finds the same idempotency keys) and the `gold_eval` envelope.
7. A repaired release needs a new freeze. A post-test regression run requires `--post-test-regression` and `--reason`; a new reliability claim needs a newly drafted, independently sealed test set.

## Owner steps on the local host

1. Pull, run `check --phase all --provider fake --save`. The first command migrates the ledger to schema 6 in place.
2. Draft and review gold-2 rows: development on 질문 검토, sealed test in the terminal (`.wiki/gold-drafting.md`, "Phase 4 gold rows"). Validate and freeze both splits.
3. Rerun `evaluate-retrieval --dataset dev` (D/H reuse cached vectors), compare, activate the selection with its finalist.
4. `plan-run --action answer-finalists`, review the maximum, `run-answers` with the UI stopped; export, review and import the blind sheet.
5. `freeze-release`, `plan-run --action sealed`, `run-answers` once.
6. If the budget allows, `plan-run --action latency` (5 × 6) and `latency-run`.
7. `backup` to an owner-controlled directory outside the runtime, then `restore-check`.
8. Record the mentor walkthrough (runbook §10.6), run `release-report --latest`, and copy the sanitized result into `docs/operations/release-report.md`.

## Open items

- No real-corpus gold, answer run, sealed evaluation or latency sample exists yet; every quality number above is synthetic and must not be reported as a result.
- The phase-3 host decisions (network exposure, six real browsers, warm/cold latency) remain open.
- The answer scorer's number/date extraction covers the formats in the fixture and common Korean RFP spellings (`130,000,000원`, `1억 3천만 원`, `130백만원`, `2024. 6. 11.(화) 17:00`, `오후 5시`); other phrasings fall to `missing`/`needs_review` and should be checked during blind review.
