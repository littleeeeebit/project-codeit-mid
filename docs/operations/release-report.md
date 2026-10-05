# Phase 4 release evidence

Status on 2026-10-02: limited local pilot, merged. Independent round 27 approved reviewed head `f33863d` with no new P0/P1 findings; its source was integrated unchanged into PR #8, which is merged. This does not certify unrestricted deployment or full phase-4 exit.

The selected candidate retains all development/sealed failures and the original-source fallback. The owner reported a pass after viewing the app; this is limited-pilot acceptance, without invented individual actions or human gold-review calibration. Development and first frozen sealed review, real latency and pilot-app recovery are complete. The pilot ran in its own runtime; the live runtime was migrated separately (see the last section). Central runner registration was done on 2026-10-05 (see the last section).

This is an AI-reviewed pilot, not human-reviewed 120-question gold. Private originals, questions, labels, responses, keys and runtime databases remain outside Git.

## Completed checks

| Check | Actual outcome | Scope |
| --- | --- | --- |
| `check --phase all --provider fake --save` | 262 tests passed, two platform skips; Chromium flow passed, 325.827 seconds | Windows host, package source `b8613fe6a707c49ae3bd70c381514556c0fda840c146713a4950502eb28b67ec` |
| Independent round 24 | No new code findings; four targeted tests and integration fixture passed | Conflict action persists through execution, review import and release scoring; citation ownership guards retained |
| Original integrity | All 98 source hashes match; 100 associations parsed | Read-only corpus, not corpus-wide human fidelity approval |
| Development labels | 50 approved rows; alternative-support and conflict corrections independently reviewed | API Luna drafts and source-based AI review; sticky disagreement survives later agreement until corrected revision |
| Frozen test pilot | 14 approved rows, two each of seven types | Three families; source and disputed second reviews completed; first sealed run executed |
| Accepted active-app migration/recovery | Schema 6 before/after idempotent initialization; all 18 fresh recovery checks passed; 285 mutable files; live ledger unchanged, staged paid generation disabled | Actual running app used for real-provider calls and owner acceptance; independently checked in round 27. Earlier 279-file recovery is retained as history |
| Local verification settings | Saved, schema validated, UTF-8 without BOM | Registered with the local verification service on 2026-10-05 (last section) |
| Owner pilot acceptance | Owner reported a pass after viewing the app in this session | Limited-pilot usability acceptance; no per-step detail supplied, no claim of six participants or human gold calibration |
| Fresh deployment-copy migration/recovery | Schema 3 to 6; all 18 existing tables retain their original values and counts; repeat migration unchanged; integrity and foreign-key checks pass; 18 recovery checks pass | Copy of the live DB/WAL and mutable files; live files unchanged, zero paid calls. The in-place migration followed later (last section) |

The earlier full gate encountered one Windows junction file-lock error. Its failed attempt remains recorded; subsequent complete gates passed. Package source is the compatibility boundary: historical answers, estimates and checks are not relabeled as current after code changes.

## Development experiments

The initial comparison held model, prompt, 2,000-token output cap and evidence budget fixed. Run `A-e94df20a18c3` completed 100 calls for $0.110934. Independent review covered 176 unique outside-label question/passage pairs and all 1,361 answer-review items. No retries or unknown billing occurred.

| Initial finalist | Correct required claims | Complete questions | Supporting citations | Critical wrong values | Technical outcomes |
| --- | --- | --- | --- | --- | --- |
| K0 whitespace | 169/250, 67.6% (Wilson 95% 61.6–73.1%) | 28/50 | 199/200 | One performance reversal | Two |
| K1 morphological | 224/250, 89.6% (Wilson 95% 85.2–92.8%) | 44/50 | 279/279 | None observed | Three |

Supported citations do not establish complete answers. Both finalists missed the 90% claim-correctness target. Refusals, omissions and the reversed assertion remain failures. These historical results are not final-candidate or sealed scores.

Six alternate supports were applied through five atomic label revisions, retaining unrelated conditions and exceptions. The verified commercial-use conflict preserves both incompatible clauses without inventing priority. Current free retrieval recorded K0 `K0-99e5c82f9a` with multi-group complete@20 85.42%, packed completeness 76%, nDCG@5 0.6696 and MRR 0.6647; K1 `K1-cf31abec51` recorded 100%, 100%, 0.9500 and 0.9612 respectively. Both had single-group hit@20 100%. The pinned source-span nDCG does not equate general topical relevance with complete support. All remaining outside-label pairs have independent source verdicts: 113/69 occurrences, 168 unique pairs. Different label versions are different populations.

Separate 4,000-token development experiments preserve the fair initial comparison. Historical prompt-3 run `A-6658da7ad284` cost $0.058866; independent round 22 reviewed all 751 items and 50 contexts. It scored 215/246 required facts, 41/49 complete positive questions and 280/281 supporting citations, with one critical performance reversal and four technical outcomes. The conflict action was null. That candidate was rejected before freezing or sealed execution.

Generic prompt `grounded-answer-4` adds atomic obligation directions, explicit form placement, completeness and purchaser clarification, plus a document-to-evidence map for Luna. Synthetic examples contain no corpus or sealed answers. The scoring presence check rejects missing conflict actions; independent review must still establish semantic adequacy. The producer repair preserves the actual action instead of weakening that check.

Current run `A-9ef59b566d64` completed 50 calls for $0.059848: 47 answered, two insufficient-evidence outcomes and one conflict outcome, with zero technical errors or unknown billing. Independent round 25 reviewed all 750 blind items and all 50 full contexts; exact verdicts were imported once with ID/hash/coverage validation.

| Current development metric | Actual result, Wilson 95% interval |
| --- | --- |
| Correct required claims | 225/246, 91.46% [87.30%, 94.35%] |
| Complete positive questions | 45/49, 91.84% [80.81%, 96.78%] |
| Supporting citations | 268/272, 98.53% [96.28%, 99.43%] |
| Expected negative status | 0/1, 0% [0%, 79.35%]; target failed |
| Wrong/unresolved required-critical verdicts | Zero; this does not erase omissions or supplementary summary defects |

The expected conflict accurately preserves both clauses and requests purchaser clarification, but returns the wrong `answered` status. Two unnecessary refusals, incomplete subcontract conditions, one missing deliverable and four one-sided comparison citations remain. One summary wrongly extends a large-content delay-notification obligation to the normal-response case; that supplementary context finding has no exported blind item. Another answer invents a conflict between compatible security-document requirements. All 233 exported generated assertions are source-supported, a narrower result than approval of every summary and condition. These failures remain explicit; no labels or historical verdicts were rewritten to improve scores.

The selection is limited: outputs and summaries remain drafts requiring original-source verification before contractual decisions. Use free original/verifier tools for conflicts, incomplete answers and comparisons, checking both documents and every required condition. The negative-handling target has not passed. The smaller AI pilot and these fallback decisions establish no unrestricted deployment or merge approval.

## Frozen sealed evaluation

Freeze `F-f7a3a9bee3` pins clean documentation revision `1fd2bf3`, unchanged package source, prompt/model/settings, source/index/metric identities, dataset manifests and review logs. It recorded `post_test=false` before dispatch. First run `S-95b3004bd1a8` completed all 14 calls for $0.016929, with every outcome `answered`, no retries and no unknown billing. The first exposure is retained in the audit history. Independent round 26 reviewed all 211 answer items, all 14 full contexts and all 28 outside-label top-five passage pairs; its exact answer verdicts were imported literally once. No tuning, relabeling or rerun after test exposure occurred.

| First sealed metric | Actual result, Wilson 95% interval |
| --- | --- |
| Correct required claims | 55/56, 98.21% [90.55%, 99.68%] |
| Complete questions | 13/14, 92.86% [68.53%, 98.73%] |
| Supporting citations | 83/84, 98.81% [93.56%, 99.79%] |
| Exported assertion support | 70/71; one comparison lacks the second source at its attached citation |
| Negative handling | Not applicable, 0 eligible cases; not 100% |

One education comparison omits the purchaser-requested additional-training support obligation and cites only one institution for a two-institution assertion. The missing mandatory condition and unsupported assertion/link remain failures. No wrong or unresolved required-critical value was recorded; that narrower metric does not credit the omitted obligation. These small, family-limited counts and intervals do not establish broad contractual reliability or erase the development conflict-status failure.

The sealed outside-label pool has 14 irrelevant, 12 partial and two complete alternate-support pairs. The two alternates are genuine source-table support absent from the frozen label coordinates. The predeclared source-span ranking metric consequently undercredits them; a canonical zero is not proof of semantic irrelevance. Their exact source judgments are preserved separately. Frozen labels, metric rules, ranks and the first-test scores were retained.

## Latency and budget

The current-source, 4,000-token real-provider sample ran five waves of six simulated concurrent members: 30 requests, zero failures, overall p50 6.38 seconds and p95 10.53 seconds. Warm n=24 had p50 5.73 seconds and p95 10.39 seconds, below the 15-second target; the first wave took 10.60 seconds. This is a preliminary sample, not six human participants or an SLA.

The pilot runtime ran under a $3 operating cap within the $5 allowance. After the first sealed run and UI walkthrough, its ledger showed $0.609441 settled, $0 pending, $0 unknown and $2.390559 available under that cap. That includes the 105 attempts ($0.160623) it inherited from the live ledger snapshot; the pilot itself added 432 attempts, $0.448818. This is the app ledger priced at stored rates, not provider balance/invoice reconciliation, and no provider reconciliation watermark is recorded. Gateway ownership, purpose envelopes and atomic admission checks apply to every paid run.

The current-build AI walkthrough completed one metered consultant answer for $0.000518, opened its scoped source context and verified the original bytes/hash. A separate free verifier trace used the approved development question's automatic document scope, 12 evidence units and 4,219 tokens in 4.5 milliseconds; paid verifier generation remained unconfirmed. No browser download wrote outside the pilot checkout. This establishes an AI walkthrough, not human launch/recovery participation.

## Acceptance and limits

The owner's statement, "Based on the results I have seen, it is a pass," closes the requested limited-pilot usability acceptance. No further review of all 50 development questions is requested from the owner. It does not establish human/source judgment calibration or another member's independent launch/recovery rehearsal; those remain limits on a full release claim rather than evidence this pilot performed them.

The test pilot has seven early and seven middle source positions, no late positions, no verified absence/conflict stratum and no unanswerable rows. False-premise rows are answerable corrections. It cannot establish general negative-handling quality or strong release reliability. Automated walkthroughs and simulated load do not establish human participation.

## Live runtime migration and pilot archive

Before the in-place migration, a rehearsal on a copy of the live DB/WAL and mutable files went from schema 3 to 6. It preserved 645,927 rows across 18 tables and 13 mutable files, was idempotent on a repeat, passed integrity and foreign-key checks, and passed all 18 staged recovery checks.

The in-place migration ran on the owner host on 2026-10-02, with the app stopped:

1. `backup` of the live ledger before migrating (`C:\Users\dasdk\rfp-backups\2026-10-02-pre-schema6`; ledger revision 316, 105 settled attempts, $0.160623).
2. `init --paid-disabled`: schema 6, `quick_check` ok, budget settings and spending unchanged. `init` keeps existing budget settings, so paid use stays as it was.
3. `backup` again (`C:\Users\dasdk\rfp-backups\2026-10-02-schema6`) and `restore-check` on it: passed.
4. The pilot's own spending was recorded as owner adjustment `external:pr8-pilot-ledger`, $0.448818, citing the pilot ledger. The live ledger now shows $0.609441 spent, which matches the pilot ledger, with the $5 cap kept by owner decision ($4.390559 available).

The pilot database was not copied over the live one; the two histories stay separate. On 2026-10-05 the pilot database (schema 6: 181 gold candidates, the frozen sealed set and its first run `S-95b3004bd1a8`, answer runs including `A-9ef59b566d64`) was imported into its own PostgreSQL database, `bidmate_pilot_archive`, with paid admission disabled, and validated table by table against the source (see the [handover](../../handoff/postgresql-pgvector/README.md#pilot-archive-2026-10-05)). Its run, review and receipt files stay archived, gitignored, under `.runtime/live-validation/pr8-review-archive/62015a9-owner-setup/`. Do not publish their payloads or credentials. Use [runbook sections 10–12](runbook.md) for planning, paid execution, freeze, read-only reporting and paid-disabled recovery.

## Judge comparison: Luna versus Jev (2026-10-04)

Method, rule and limits are in [Luna judge versus Jev judge](../rag/judges.md). The reference is the 750 reviewed blind items of development run `A-9ef59b566d64`, copied read-only from the pilot archive after the receipt's packet and verdict hashes matched (reference `7c71e582…`, split `474cfa60…`). The sealed set and run `S-95b3004bd1a8` were not read.

Funding: the owner moved $1.00 from `interactive` to the new `judge_eval` envelope (`set-envelopes`, actor `owner`), leaving embedding $2.50, gold_eval $2.50, interactive $4.00 and judge_eval $1.00 within the unchanged $10 cap.

| Run | Part | Estimate (max) | Actual (ledger) | Outcome |
| --- | --- | --- | --- | --- |
| `J-calibration-637edadd55aa` | calibration, 373 items | $0.499313 | $0.093721 | complete; thresholds `b0670027…` fitted, refitted as `d9c0e8ac…` (no new calls) |
| `J-held_out-3e072719113f` | held-out, 377 items (raw sample 120) | $0.373294 | $0.061321 | complete; superseded, see below |
| `J-held_out-5b18a0afb471` | held-out, 377 items (raw sample 120) | $0.338342 | $0.053900 | complete; the reported verdict |

Review round 1 found that the threshold fit measured coverage only over Jev's answered calibration items. Failed and untranslatable items were left out, so the fitted bands claimed 100% coverage where the true figure was 94.8% (support) and 91.7% (coverage). The fit now divides by every eligible item, and `judge-refit` refitted it from the stored judgements without any call. The bands came out unchanged and all of them still meet the 90% floor, but the thresholds hash changed. A held-out run's identity includes that hash, so the first held-out run no longer matched the configuration and was run again.

The second held-out run needed no translation: every segment was cached. It paid for 377 Luna calls only. The three runs together cost $0.208942, all of it in `judge_eval`. Afterwards the ledger showed $2.126419 spent, $0 pending and $0 unknown of the $10 cap, with $0.791058 left in `judge_eval`. The 1,410 Jev calls (470 per run) are unpriced: TypeSafe reports usage but no price. An earlier version of this section gave 847 for the first two runs; the judgement files show 940.

Held-out result under `replacement-rule-1` (run `J-held_out-5b18a0afb471`): **not replaceable**. The false-accept condition decided it.

| Arm | Judged / items | Agreement (Wilson 95%) | Kappa (pass/fail) | False accepts / reference negatives | p50 / p95 latency |
| --- | --- | --- | --- | --- | --- |
| Luna | 377 / 377 | 89.1% (85.6–91.9) | 0.240 | 5 / 13 | 2.09 s / 4.37 s |
| Jev, bridged | 350 / 377 (92.8%) | 89.1% (85.5–92.0) | 0.238 | 6 / 13 | 0.24 s / 0.30 s |
| Jev, raw Korean | 120 / 120 | 80.0% (72.0–86.2) | 0.103 | 1 / 3 | 0.23 s / 0.27 s |

- kappa 0.238 ≥ 0.240 − 0.05: passed.
- false accepts 6 ≤ 5: failed.
- coverage 92.8% ≥ 90%: passed.

The superseded run had reached **replaceable** with Luna at 7 false accepts and kappa 0.189. Between the two runs Jev's coverage and false accepts did not move. Luna, called again on the same items with the same prompt, passed two fewer reference negatives. The verdict therefore turns on one or two of the 13 negatives and on Luna's run-to-run variation. Neither run gives Jev a margin. With this reference Jev is not shown to be a safe replacement, and the rule says so.

Jev's 27 abstentions are all bridge rejections: 21 `changed_protected_value` and 6 `residual_hangul`. The bridge lifts Jev's kappa from 0.103 to 0.238. The raw-Korean arm has only 3 negatives in its sample.

### The PostgreSQL work these runs ran on (2026-10-03/04)

- The live app served from PostgreSQL `bidmate_app` as the authoritative database. The authority marker was installed at 2026-10-04 02:06 UTC after the final validated import; the previous database's ledger stayed a read-only cold archive until its files were deleted on 2026-10-05. Every judge and translation call was admitted and settled on that PostgreSQL ledger.
- The app had been started with the fake provider. For these runs it was restarted on 8501 with the real provider and the same database. That restart changed no budget settings.
- Judges never retrieve, so serving configuration played no part. The PR #12 fixed-1,536 dense/hybrid configuration stays refused. Since PR #13 the app serves hybrid run `H-0fffb2a6ec` with fusion `keyword_first:60:1.0:6`, which passed its gate against K1.
- `/api/verify/fidelity` failed on PostgreSQL with a `GROUP BY` error, which blanked the whole 검증 page. It was fixed in `7cbe847` and checked live: 94 rows for 94 HWP sources.

## The three answers that failed the PR #13 rerun (2026-10-05)

PR #13's rerun of 26 paid answers left three failures. Their run records showed these causes:

- `refresh50-ad-migration-design` ended `output_truncated`. The 2,000-token output cap includes reasoning tokens, and reasoning used it up before the 13 required evidence groups were written.
- `refresh50-eg-input-error` and `refresh50-dh-linked-data` failed with `evidence_scope_mismatch`. An inference that compares the two documents needs evidence from both, and validation only let a claim cite its own document. The model had no valid way to cite such an inference.

Fixes, each with a fake-provider regression test that fails without it:

- The output cap is 4,000 tokens, the released candidate's cap (`tests.test_generation`).
- Prompt `grounded-answer-10` and validation let a comparison inference cite the other compared document, as long as it also cites its own. Source facts still cite only their own document (`tests.test_service.ModesTest`).
- The first paid rerun showed a third cause for `refresh50-ad-migration-design`: serving halved the evidence limits per comparison side, so it retrieved 4 of 13 groups, while the retrieval gate had measured each side with the full single-document limits. Each comparison side now gets the single-document limits the gate measured (`tests.test_service.ModesTest`). The owner chose this serving change in this task; `H-0fffb2a6ec`, its fusion and exact search are unchanged.

A row now passes only when its status is the expected one, it has no technical failure, and an answerable row cites every gold group retrieval reached (and retrieval reached at least one). Reached and cited are graded on the chunks' source spans against the approved occurrence, as retrieval is; review round 1 found the first version matched elements only. Re-scored this way, both runs below keep their results. `plan-run --question-id` limits an answer run to listed rows.

| Run | Rows | Result | Settled cost |
| --- | --- | --- | --- |
| `A-567426a3adce` | the three rows | 2/3: `eg` and `dh` passed; `ad` `insufficient_evidence` with 4 of 13 groups retrieved | $0.003653 |
| `A-863087b6337f` | the three rows, after the packing fix | 3/3: `ad` 13/13 groups, `dh` 5/5, `eg` 2/2 | $0.005071 |

Both runs were estimated with `plan-run` and approved by the owner before any paid call. Together they cost $0.008724. The ledger afterwards (`budget-report`, revision 5367): $2.136133 spent, $0 pending, $0 unknown, $7.863867 available. The 23 rows that passed in PR #13 and the sealed set were not rerun.

## Central runner registration (2026-10-05)

`verification.json` now lists Phase 5's plan as a contract, adds a `phase5-gate` assertion to `repository-gates`, and includes the read-only `budget-report` test in `budget-recovery`. The owner settings were saved through the local review settings on 2026-10-05 and are bound to that manifest's digest (`dc33e9d8…`); `.wiki/verification.local.json` stays git-ignored. The managed flow `python -B tools/verify.py repository-gates` then ran at head `a2e879a`, and its local-evidence block passed every assertion:

| Assertion | Actual |
| --- | --- |
| whitespace | no whitespace errors |
| phase3-gate | 71 tests OK, one skip |
| phase4-gate | 72 tests OK |
| phase5-gate | 55 tests OK |
| full-suite | 363 tests OK, three skips |
| web-checks | lint and typecheck exit 0 |
