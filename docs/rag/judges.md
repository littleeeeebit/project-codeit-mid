# Comparing a Luna judge with a Jev judge

The question: can the Jev classifier (TypeSafe, `jev-1.13.0`) replace an LLM-as-a-judge built on `gpt-6-luna` for reviewing development answers? Both judges are measured against the same independent blind review, and a rule fixed before the measurement decides. The code is `src/rfp_assistant/judges.py`; the screen is 검증 → 판정 모델 비교.

This page records the method as declared before any held-out run. The result section at the end is written after the run and does not change anything above it.

## Reference

The reference is the blind review sheet of development answer run `A-9ef59b566d64`, 750 items. Independent round 25 reviewed every item, and its verdicts were imported once (see the [release report](../operations/release-report.md)). It is the only fully reviewed, blind, development-scope verdict set. The sealed test set and its run `S-95b3004bd1a8` are never read, judged, tuned against or shown.

`python -m rfp_assistant.cli judge-reference` copies the reference read-only from the archived pilot runtime into the live runtime (`.runtime/judges/reference/`). It first checks the review receipt: the verdict file and blind packet hashes, the 750-item count, and that every imported verdict equals the reviewed verdict file. The manifest records every source file's hash. If anything is missing or differs, the command stops; the owner decides before another reference is used. A different reference already installed is never replaced silently.

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

Runs resume: finished judgements are kept, and only the remainder runs again under a new estimate. The run stops at the first budget refusal or unknown billing, and a blocked run stays partial, labelled with its status.

## Limits

- The held-out part holds 13 negative verdicts: 2 unsupported links, 9 missing and 2 incomplete qualifiers. False accepts and kappa rest on those few items. A difference of one false accept decides condition 2, and kappa moves sharply with one item.
- The reference is itself an independent AI review (round 25), not human gold. Agreement measures consistency with that reviewer, not truth.
- The bridge hides institution names behind opaque labels. Jev can tell two institutions apart but cannot use the name's meaning.
- Only names in the corpus's purchasing-institution metadata are masked. A name spelled differently, or an organisation that is not a purchaser, is translated like other text. One held-out item renders 봉화군 as both "Bonghwa-gun" and "Bonghwa County". Such a name is not checked for alteration.
- Jev is unpriced: TypeSafe returns token usage but no price.

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

**Verdict: not replaceable.** False accepts decided it:

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
