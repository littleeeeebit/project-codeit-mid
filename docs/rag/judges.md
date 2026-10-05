# Comparing a Luna judge with a Jev judge

The question: can the Jev classifier (TypeSafe, `jev-1.13.0`) replace an LLM-as-a-judge built on `gpt-6-luna` for reviewing development answers? Both judges are measured against the same independent blind review, and a rule fixed before the measurement decides. The code is `src/rfp_assistant/judges.py`; the screen is 검증 → 판정 모델 비교.

This page records the method as declared before any held-out run. The result section at the end is written after the run and does not change anything above it.

## Reference

The reference is the blind review sheet of development answer run `A-9ef59b566d64`, 750 items. Independent round 25 reviewed every item, and its verdicts were imported once (see the [release report](../operations/release-report.md)). It is the only fully reviewed, blind, development-scope verdict set. The sealed test set and its run `S-95b3004bd1a8` are never read, judged, tuned against or shown.

`python -m rfp_assistant.cli judge-reference` copies the reference read-only from the archived pilot runtime's run files into the live runtime (`.runtime/judges/reference/`). The same 750 items, with their verdicts, are also queryable in the pilot's PostgreSQL database, `bidmate_pilot_archive` (table `pilot_review_items`). It first checks the review receipt: the verdict file and blind packet hashes, the 750-item count, and that every imported verdict equals the reviewed verdict file. The manifest records every source file's hash. If anything is missing or differs, the command stops; the owner decides before another reference is used. A different reference already installed is never replaced silently.

| Item kind | What it asks | Reference labels (all 750) |
| --- | --- | --- |
| `link` | Does one cited passage support the answer claim that cites it? | 268 supporting, 4 unsupported |
| `answer_claim` | Do a claim's cited passages together support it? | 233 supported |
| `claim` | Does the answer state a required gold fact with its conditions? | 225 correct, 17 missing, 3 incomplete_qualifier |

Only 24 of the 750 verdicts are negative. Every negative count below is therefore small, which limits what any rule over this reference can establish (see Limits).

## Calibration and held-out parts

The items are split once, with seed 20261004, stratified by item kind and reference verdict. Within each stratum the shuffled IDs are halved, so the calibration part has 373 items and the held-out part 377. A raw-Korean control sample of 120 items is drawn from each part with seed 20261005. The ID lists are persisted in `.runtime/judges/split.json` with their hash. Every read checks the file against that hash and against the seeded draw from the installed reference. A changed file is refused even if its hash field was left alone or recomputed; it is never redrawn.

The calibration part is used for two things only: fitting Jev's thresholds, and checking the Luna prompt. Every number on the comparison screen comes from the held-out part.

The judge prompts were revised once after reading the calibration reviewers' notes, and only those notes. Two reviewer rules were not stated in the first draft:

- a link supports a claim when it states one component of a multi-passage claim for the right organisation;
- an answer that declines to answer earns no required fact, even when it quotes related text.

## Arms

| Arm | Reads | Decides |
| --- | --- | --- |
| Luna | English instructions; Korean question, answer and evidence as JSON data | A reference label and a one-sentence reason, through strict structured output |
| Jev, bridged | English only, from the checked bridge below | A support probability per item; for required facts also a Choice among correct, incomplete_qualifier and missing |
| Jev, raw | The same questions over the untranslated Korean, on the 120-item sample | The same, as a control for what the bridge adds |

Neither judge sees finalist names, retrieval modes, reviewer notes or the deterministic pre-check that the sheet carried. Both receive the same fields:

- `link` items: the question, the claim and the cited passage.
- `answer_claim` items: the question, the claim and every passage it cites (recovered from the run's stored evidence).
- `claim` items: the question, the required fact and conditions, the gold source passages, and the answer's summary and claims.

### Luna judge prompt

`JUDGE_INSTRUCTIONS` in `judges.py` (version `luna-judge-1`) states each kind's rule:

- `link`: `supporting` if the passage states at least one part of the claim for the organisation or document the claim attributes it to, contradicting none of it; otherwise `unsupported`.
- `answer_claim`: `supported` only if the cited passages together state the whole claim, every number, condition, negation and scope included.
- `claim`: `correct` (stated with every listed condition), `incomplete_qualifier` (a condition omitted or changed), `wrong_value` (a different value) or `missing`. An answer that declines to answer is `missing`.

Calls go through the shared gateway: atomic admission, durable dispatch and settlement. They are charged to the `judge_eval` envelope, at the configured reasoning effort and with a 1,500-token output cap. A provider rejection before execution is a failed call and an abstention; unknown billing stops the run and is never replayed.

### Korean → English bridge

Jev's Korean reading is poor, so Jev never receives Hangul. The bridge is gpt-6-luna with the committed procurement glossary (`src/rfp_assistant/judge_glossary.json`):

1. Protected values become placeholders `[[Vn]]` before translation. Each is rendered as follows when restored:
   - dates: ISO `2024-06-11 17:00`;
   - requirement and notice codes: as written;
   - purchasing-institution names from the corpus metadata: a stable opaque label `Org-XXXX`, the same for the same institution everywhere;
   - amounts with a Korean magnitude: plain digits (`1억 3천만` → `130,000,000`);
   - every other digit run: as written.
2. Unique segments are translated in batches (at most 12 segments or 3,500 characters), with reasoning off.
3. Each translation is restored and rejected as untranslatable if:
   - a placeholder is dropped, duplicated or invented (`changed_protected_value`);
   - any digit appears outside a placeholder (`changed_number`);
   - any Hangul remains (`residual_hangul`).
4. Results are cached by the hash of bridge version, glossary, model and masked text (`.runtime/judges/bridge/`), so a segment is paid for once. A cached record whose stored source, model, version or glossary hash differs from its key counts as a miss, and the segment is translated again. Every read re-runs step 3.

An item with any untranslatable segment is not sent to Jev; it counts as an abstention. `tests/test_judges.py` covers placeholder restoration, residual-Hangul rejection and changed-number rejection.

### Jev questions and thresholds

The client is a small HTTPS client in `judges.py`, not wiki-agent's package. It reads `TYPESAFE_API_KEY` from the environment or `.env`; the model is the `jev_model` setting (default `jev-1.13.0`). Each request is one Noul question:

- `link`: does the passage state at least one part of the claim for the right organisation?
- `answer_claim`: do the passages together state the claim?
- `claim`: does the answer state the required fact with its conditions? Required-fact items add a Choice among correct, incomplete_qualifier and missing.

Probabilities become labels through a band fitted on the calibration part, per arm and per question family. The families are support (link and answer_claim together, since answer claims have no negative reference verdict) and coverage (required facts).

- At or above `hi`, the label is positive.
- Below `lo`, it is negative. For required facts, the Choice then picks between incomplete_qualifier and missing.
- Between the two, the item abstains.

The fit maximises binary Cohen's kappa among bands that keep at least 90% of calibration items judged. Ties go to fewer false accepts, then more coverage, then the narrower band.

Coverage is measured over every calibration item the arm was asked about. Failed calls and untranslatable items count against it; only code-settled items are left out. Each band records `n`, `points` and `meets_floor`.

A held-out run is refused in two cases:

- the stored `thresholds.json` differs from a fresh refit of the calibration judgements;
- any band missed the 90% floor.

`judge-refit` refits for free from the stored judgements.

An uncertain probability, an API failure and an untranslatable item are all abstentions, never negative verdicts.

### Deterministic value checks

Value correctness is decided by code for every arm, before any judge's label, so neither probability nor prose decides a number:

- A required fact the typed check found stated with another value is `wrong_value`.
- A support item whose claim states a date or a Korean-magnitude amount absent from every cited passage is `unsupported`.

On the calibration part these checks settled no item. They are reported per arm as "code-settled" on the held-out part.

## Metrics

Each arm is measured on the held-out part (Jev raw on its 120-item sample):

- **Coverage**: the share of items judged rather than abstained.
- **Agreement**: exact-label agreement with the reference among judged items, with a Wilson 95% interval.
- **Kappa**: Cohen's kappa on pass (supporting, supported, correct) versus fail. A kappa pooled over the three kinds' label sets would be inflated, because the item kind alone predicts the label family.
- **Confusion matrix**: per kind, reference label against judge label, with abstentions as a column.
- **False accepts**: items the judge passes where the reference fails them.
- **USD**: Luna's judge calls and the bridge's translation calls are read from the shared ledger separately. Jev uses the provider-reported cost when every call reports one; otherwise it is shown as unpriced, with the call count.
- **Latency**: p50/p95 per call, sequential, one call at a time.

Each run's `config.json` records:

- the reference and split hashes;
- the prompt, glossary, Jev-question and rule hashes;
- the organisation list hash;
- the models;
- the thresholds hash.

`results.json` holds the metrics and the verdict.

## Replacement rule (declared before any held-out run)

`replacement_verdict` (rule `replacement-rule-1`) compares bridged Jev with Luna:

- **Inconclusive** when either judge judged fewer than 100 held-out items, or kappa is undefined.
- **Replaceable** only if all three hold:
  1. `kappa_Jev >= kappa_Luna - 0.05`;
  2. Jev false accepts ≤ Luna false accepts;
  3. Jev coverage ≥ 90%.
- **Not replaceable** otherwise. The screen names the failed conditions as the deciding ones.

False accepts are compared directly because they are the failure that would pass wrong answers. Plain agreement would hide abstentions and the class imbalance.

## Operation

1. `judge-reference` installs the reference and split (free).
2. The owner funds the `judge_eval` envelope with `set-envelopes`. It starts at zero, and an unfunded envelope blocks the plan rather than borrowing from another purpose.
3. On the screen, or with `plan-run --action judge-comparison --part calibration`, estimate the calibration part. The estimate counts:
   - translation attempts;
   - judge attempts, which are never zero;
   - Jev calls, as a count;
   - the maximum USD.

   A changed configuration, price, token count or cached translation invalidates the estimate, and the run must be planned again.
4. Start calibration. A complete calibration run writes `thresholds.json`. After a change to the fitting rule, `judge-refit` rewrites it from the stored judgements, at no cost.
5. Plan and start the held-out part. Its identity includes the thresholds hash.

A start first checks the estimate and inputs and writes the run's `config.json`, all before it returns. Only then do the paid calls begin, in one background thread. As a result, the progress screen lists the run as running from its first refresh. The development answer evaluation starts the same way.

Runs resume: finished judgements are kept, and only the remainder runs again under a new estimate. The run stops at the first budget refusal or unknown billing, and a blocked run stays partial, labelled with its status.

## Limits

- The held-out part holds 13 negative verdicts: 2 unsupported links, 9 missing and 2 incomplete qualifiers. False accepts and kappa rest on those few items. A difference of one false accept decides condition 2, and kappa moves sharply with one item.
- The reference is itself an independent AI review (round 25), not human gold. Agreement measures consistency with that reviewer, not truth.
- The bridge hides institution names behind opaque labels. Jev can tell two institutions apart but cannot use the name's meaning.
- Only names in the corpus's purchasing-institution metadata are masked. A name spelled differently, or an organisation that is not a purchaser, is translated like other text. One held-out item renders 봉화군 as both "Bonghwa-gun" and "Bonghwa County". Such a name is not checked for alteration.
- Jev is unpriced: TypeSafe returns token usage but no price.

## Judge golden set (mutated correct answers)

The held-out part's 13 negatives cannot separate two judges. The judge golden set adds negatives whose labels are known by construction: `src/rfp_assistant/judge_set.py` takes the held-out part's approved positives and changes each one in a known way. No person reviews the mutants. Calibration items are never mutated, so the fitted thresholds never see them.

`python -m rfp_assistant.cli judge-set` writes `.runtime/judges/judge-set/items.jsonl` and `manifest.json` (version `judge-set-1`, seed 20261005). For the same reference, split and generator, it reuses the file. The set is stored and counted apart from the RAG development set. `golden-counts` reports it in its own section, and the 판정 모델 비교 screen counts it in its set selector.

| Mutation | What changes | Known label |
| --- | --- | --- |
| `amount` | A stated amount or count becomes a different value (300만원 → 4,000,000원) | link/claim: unsupported; required fact: wrong_value |
| `unit` | A unit swaps (개월 ↔ 년, 원 → 달러, % → 배) | unsupported / wrong_value |
| `qualifier` | VAT inclusion flips (부가세 포함 ↔ 별도), or a comparison or obligation word after a number flips (이상 ↔ 이하, 필수 → 선택, 이전 ↔ 이후) | unsupported / incomplete_qualifier |
| `date` | A calendar date moves by 7 days, or a period or deadline moves (15일 → 22일, 12개월 → 15개월). The reference answers state no calendar dates, so periods and deadlines carry this type | unsupported / wrong_value |
| `negation` | The predicate is negated (제출하여야 합니다 → 제출하지 않아도 됩니다) | unsupported |
| `dropped_condition` | A required fact's condition is removed from the answer | incomplete_qualifier |
| `wrong_evidence` | The claim stays and its passages come from another question whose passages share at most 30% of its bigrams | unsupported |

Every mutant passes a deterministic check (`judge_set.differs`). The text must change. The new value must not also appear in the passages. A negation must add a negation, and a dropped condition must really disappear from the answer. A mutant that fails the check is dropped and counted. Each row stores its `mutation` (`type`, `from`, `to`), its `source_id` and its known `reference` label. Each mutated source also appears once, unmutated, as a positive.

The generated set has 919 items: 633 negatives (amount 19, unit 27, qualifier 85, date 18, negation 206, dropped condition 28, wrong evidence 250) and 286 unmutated positives. No held-out answer states a VAT qualifier, so the qualifier rows are comparison and obligation flips.

The run is part `judge_set` (`plan-run --action judge-comparison --part judge_set`, then `run-judges --estimate-id <id> --actor <name>`, or the screen's plan and start). It reuses the calibration thresholds, and its identity includes the set's hash. It runs Luna and bridged Jev only. Results report each judge's false-accept rate per mutation type, from the judge's own label even when a deterministic value check would settle the item, with a Wilson interval and the unmutated pass rate. The screen's set selector switches between the held-out verdict and this table. It produces no replacement verdict; the rule above stays the held-out rule.

The glossary hash that enters every run identity is computed over LF-normalised bytes, so a CRLF checkout no longer changes run identities.

## Result

Recorded 2026-10-04 from held-out run `J-held_out-5b18a0afb471`, with thresholds `d9c0e8ac…` refitted from calibration run `J-calibration-637edadd55aa`. Spend and the run table are in the [release report](../operations/release-report.md#judge-comparison-luna-versus-jev-2026-10-04).

The first held-out run, `J-held_out-3e072719113f`, is superseded. Its thresholds (`b0670027…`) had measured coverage only over answered calibration items. The refit counts failed and untranslatable items too, and gave the same bands under a new hash, so the held-out part was run again under the new identity.

Fitted bands:

| Arm | Support band (coverage) | Coverage band (coverage) |
| --- | --- | --- |
| Jev, bridged | 0.51 (94.8%, 239 of 252) | 0.69 (91.7%, 111 of 121) |
| Jev, raw | 0.83 (100%) | 0.60 (100%) |

Bridged Jev's bands both have `lo = hi`, so no uncertain band survived the coverage floor.

| Arm | Coverage | Agreement (Wilson 95%) | Kappa | False accepts | p50 latency |
| --- | --- | --- | --- | --- | --- |
| Luna | 100% (377) | 89.1% (85.6–91.9) | 0.240 | 5 of 13 | 2.09 s |
| Jev, bridged | 92.8% (350 of 377) | 89.1% (85.5–92.0) | 0.238 | 6 of 13 | 0.24 s |
| Jev, raw Korean | 100% (120) | 80.0% (72.0–86.2) | 0.103 | 1 of 3 | 0.23 s |

Verdict: not replaceable. False accepts decided it:

- kappa 0.238 ≥ 0.240 − 0.05: holds;
- false accepts 6 ≤ 5: fails;
- coverage 92.8% ≥ 90%: holds.

The superseded run reached **replaceable**, because Luna then had 7 false accepts and kappa 0.189. Jev's labels came out the same both times; Luna, asked the same questions again, passed two fewer reference negatives.

Read the verdict narrowly:

- Neither judge reproduces the reference well on failures. Both kappas are below 0.25.
  - Luna fails 11 supporting links and 21 supported answer claims, and passes 5 of the 9 `missing` facts.
  - Bridged Jev fails 5 supporting links and 20 supported answer claims. It passes 4 of the 9 `missing` facts, 1 of the 2 unsupported links and 1 of the 2 incomplete qualifiers.
- The false-accept condition turns on one or two items, and on Luna's run-to-run variation.
- All 27 Jev abstentions are bridge rejections; the bridge rejects rather than guesses. Most are `changed_protected_value`, where the translation dropped or reordered a placeholder.
- The bridge matters. On raw Korean, Jev's kappa is 0.103, and it fails half of the supported answer claims in the sample (18 of 36).
- No item was settled by the deterministic value checks.

So the rule does not show Jev to be as safe as this Luna judge against this reference; across the two runs it is within one or two false accepts of Luna either way. It does not say either judge is a reliable reviewer. A reference with more negatives, especially human-reviewed failures, is needed before either judge replaces independent review.

## Judge golden set result

Recorded 2026-10-05 from run `J-judge_set-1561349ff3d0`: set `937c075f…`, 919 items, thresholds `d9c0e8ac…`. The run went ahead only after the owner approved the priced estimate `3437659e8e37` (maximum $0.892478). They first moved $0.20 from `gold_eval` to `judge_eval`, because the maximum exceeded the $0.791058 left there. Actual spend was $0.156263: $0.141270 for 919 Luna calls and $0.014993 for 26 bridge translation calls. The 837 Jev calls are unpriced.

False-accept rate per mutation type: the share of judged mutants the judge itself passed. The last row is the pass rate on the unmutated sources, where higher is better.

| Mutation | Items | Luna (Wilson 95%) | Jev, bridged (Wilson 95%) |
| --- | --- | --- | --- |
| amount | 19 | 10.5% (2/19; 2.9–31.4) | 10.5% (2/19; 2.9–31.4) |
| unit | 27 | 14.8% (4/27; 5.9–32.5) | 42.3% (11/26; 25.5–61.1) |
| qualifier | 85 | 27.1% (23/85; 18.8–37.3) | 55.0% (44/80; 44.1–65.4) |
| date or period | 18 | 0.0% (0/18; 0–17.6) | 26.7% (4/15; 10.9–52.0) |
| negation | 206 | 2.4% (5/206; 1.0–5.6) | 28.3% (54/191; 22.4–35.0) |
| dropped condition | 28 | 35.7% (10/28; 20.7–54.2) | 88.5% (23/26; 71.0–96.0) |
| wrong evidence | 250 | 8.4% (21/250; 5.6–12.5) | 7.0% (15/213; 4.3–11.3) |
| **all mutants** | 633 | **10.3% (65/633)** | **26.8% (153/570)** |
| unmutated (pass rate) | 286 | 88.8% (254/286) | 88.8% (237/267) |

Jev judged 837 of the 919 items. All 82 abstentions are bridge rejections: 64 `changed_protected_value` and 18 `residual_hangul`.

What it shows:

- Both judges pass about 89% of the correct answers, so the gap is not a stricter or looser overall threshold. Jev misses changed meaning.
- Jev passes most answers that drop a required condition (88.5%), more than half of the flipped comparison and obligation words, and more than a quarter of the negated claims. Luna catches nearly every negation and shifted period.
- Neither judge reliably catches a dropped condition or a flipped qualifier. Luna still passes about a third and a quarter of them, so a deterministic qualifier check stays worth more than either judge there.
- Wrong evidence and changed amounts are the only types where Jev matches Luna.

This set does not change the held-out verdict, which remains under `replacement-rule-1`. It does show where the held-out reference's 13 negatives could not look: on 633 known failures, bridged Jev's false-accept rate is 2.6 times Luna's.
