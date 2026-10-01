# Release report (phase 4)

Status on 2026-10-01: **limited (provisional)**. The evaluation and release tooling of the [phase 4 plan](../plan/end-to-end/4-evaluation-and-release.md) is implemented and checked with the fake provider on a synthetic fixture corpus. **No real-corpus evaluation has been recorded**: there is no reviewed gold set, no development answer run, no sealed evaluation and no real latency sample in this repository. No quality figure below describes the real assistant.

This file is the shareable summary. The authoritative report is generated on the owner host by `release-report --latest` into `.runtime/releases/<release id>/` and replaces the "to be recorded" rows below with its own numbers. Procedure: [runbook §10](runbook.md#10-phase-4-evaluation-sealed-run-and-release).

## Decision

| Release | Status | Reason |
| --- | --- | --- |
| Real corpus (owner host) | not decided | No reviewed `dev`/`test` gold, no answer run, no freeze, no sealed run recorded. The phase 1–3 supported path (search, scoped grounded answer, evidence, originals, shared budget) remains the usable baseline. |
| Synthetic rehearsal (this branch) | `limited` | Every hard check that applies passed. Multi-evidence coverage and negative handling had no sealed denominator, there was no real latency sample, and the sealed set was a 1-row pilot. |

`ready` requires every hard check to pass and every quality target to be met on a sealed evaluation of reviewed gold (60 + 60). A smaller reviewed set is reported as a pilot and can at best yield `limited`.

## What was run, and where

| Check | Where | Outcome |
| --- | --- | --- |
| `check --phase 4 --provider fake` | Cloud, temporary state | 59 tests passed |
| `check --phase all --provider fake` | Cloud, temporary state | 247 tests passed |
| Phase-4 CLI walkthrough (`tools/verification/phase4_walkthrough.py`) | Cloud, fixture corpus, fake provider | 31/31 steps passed |
| Browser check of the 평가 tab and gold-2 review | Cloud, headless Chromium, synthetic runtime | rendered; approval refused without the inspection attestation; no horizontal scroll at 390 px |
| `check --phase all --provider fake --save` → `release-report --latest` | Local reviewer, Windows, fixture runtime, on `5e281a0` (before the F9 change) | 244 passed, 2 POSIX skips of 246 (browser flow ran); the host-local check counted as `pass`; fixture decision `limited` (not an owner check) |
| Paid calls | — | none; $0 spent |

Evidence: [handoff/phase4](../../handoff/phase4/README.md).

## Release manifest (to be recorded on the owner host)

| Item | Value |
| --- | --- |
| Code | Git revision of the merged phase-4 branch; `release-report` records whether the working tree differed |
| Active retrieval | the owner's `activate-run` selection; until then the keyword default `kiwi_bm25` (no dense, no reranker) |
| Model, prompt, output cap | `gpt-6-luna`, `grounded-answer-3`, 2,000 tokens (reasoning `low`) |
| Rates | `openai-standard-gpt-6-luna-2026-09-30` (recheck before paid work) |
| Datasets | `dev` and `test` manifests (hashes, rows, label, review log) from `freeze-dataset` |
| Freeze | `F-…` from `freeze-release` |

## Coverage (to be recorded on the owner host)

The phase-2 local baseline (`handoff/phase2/baseline/`) recorded all 100 associations with a status, 98 parsed sources and the two former converter failures parsed through native-print recovery but unreviewed. `release-report` recomputes parsed/reviewed/quarantined counts, human-checked sources and metadata conflicts from the live runtime.

## Evaluation (to be recorded on the owner host)

| Metric | Target | Development | Sealed |
| --- | --- | --- | --- |
| Single-evidence hit@20 | ≥ 0.90 | to be recorded | to be recorded |
| Multi-evidence complete coverage@20 | ≥ 0.80 | to be recorded | to be recorded |
| Required-claim correctness | ≥ 0.90 | to be recorded | to be recorded |
| Citation precision (lower bound) | ≥ 0.95 | to be recorded | to be recorded |
| Negative/ambiguous handling | ≥ 0.90 | to be recorded | to be recorded |
| Critical wrong deadline/amount/condition/institution | none | to be recorded | to be recorded |
| Warm full-answer p95, six users | < 15 s | — | latency sample to be recorded |

Every rate is reported with its numerator, denominator and Wilson interval; ranked metrics with family-grouped bootstrap intervals. An empty denominator is "not applicable".

## Budget

Allowance and cap on the owner host: $5 / $5 (phase-3 configuration, `README.md`). The phase-4 evaluation spends from the `gold_eval` envelope (3/16 of the cap). Settled, pending and unknown amounts, adjustments and the reconciliation watermark come from `release-report`'s `budget.json`.

## Known limitations

- Answer scoring is deterministic only for typed numbers (with their unit) and dates (with their cutoff time), matched per document; a correct value stated next to a contradicting one is contested. Text claims, and any citation whose claim is not a verbatim quote of its cited evidence, need blind human review before they count; a negated qualifier never counts as stated. Contested or unreviewed critical claims leave the critical-value hard check unverified.
- There is no paid LLM judge.
- nDCG@5 uses the predeclared source-span labels as its judged pool; passages outside every label are counted but not yet blind-reviewed; until a review is recorded, retrieval selection stays provisional (K1, no finalist).
- A two-document question has no single ranking, so it is excluded from hit@k, nDCG@5 and MRR.
- The sealed set can be evaluated once per freeze; a further run is labeled post-test regression and cannot support a new reliability claim.
