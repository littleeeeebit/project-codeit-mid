# Phase 4 release evidence

Status on 2026-10-02: limited local pilot, merged. Independent round 27 approved reviewed head `f33863d` with no new P0/P1 findings; its source was integrated unchanged into PR #8, which is merged. This does not certify unrestricted deployment or full phase-4 exit.

The selected candidate retains all development/sealed failures and the original-source fallback. The owner reported a pass after viewing the app; this is limited-pilot acceptance, without invented individual actions or human gold-review calibration. Development and first frozen sealed review, real latency and pilot-app recovery are complete. The pilot ran in its own runtime; the live runtime was migrated separately (see the last section). Central runner registration remains open.

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
| Local verification settings | Saved, schema validated, UTF-8 without BOM | Server registration remains incomplete |
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

The pilot DB was not copied over the live one; the two histories stay separate. The pilot runtime (181 gold candidates, the frozen sealed set and its first run, answer runs) and its private receipts are archived, gitignored, under `.runtime/live-validation/pr8-review-archive/62015a9-owner-setup/`; its runtime is `runtime/` there. Do not publish their payloads or credentials. Use [runbook sections 10–12](runbook.md) for planning, paid execution, freeze, read-only reporting and paid-disabled recovery.
