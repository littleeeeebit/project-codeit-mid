# Cloud's first implementation task: validate a recovered corpus

The local baseline contains 98 parsed sources and no quarantined source. Both AFSIS and MILE already have active recovery extractions. The current dataset validator nevertheless always requires `converter_unavailable`, while `RowChecker.check()` accepts that type only for a currently quarantined document. Consequently this corpus cannot supply a valid operational row to satisfy that mandatory type. Human approval of more rows alone cannot resolve this contradiction.

This is an inherited Phase 1 validation rule exposed by the recovered local corpus, rather than a regression introduced in PR #4. `baseline/validation-applicability.json` records an independent read-only checker reproduction. Its diagnostic row was synthetic and was not imported or approved.

Reproduce entirely offline using the existing evaluation fixtures: create a 24-row independently reviewed development dataset that covers every required passage/metadata type and whose sources are all parsed. Do not fabricate a converter failure or quarantine a working original. Validation currently reports the missing `converter_unavailable` type; adding such a row for a parsed document reports that its source is not quarantined.

## Minimal change to implement

In `evaluation.validate_gold()`, derive the required operational type from the current corpus state already loaded into `RowChecker.docs`. Keep passage and metadata types mandatory. Keep `RowChecker`'s strict quarantine check for any submitted converter row. The existing shared checker is sufficient; no new validator abstraction is needed.

```python
# Inside the existing open_db block, after RowChecker is created:
required_types = REQUIRED_PILOT_TYPES - OPERATIONAL_TYPES
if any(doc["parse_status"] == "quarantined" for doc in checker.docs.values()):
    required_types |= OPERATIONAL_TYPES

# Use this set for the existing missing-type check after checking the rows:
missing_types = required_types - present
```

Include the required types and whether quarantine cases were applicable in the validation report so the difference is visible. Historical failed conversions and current unresolved operational failures remain distinct. The fake-provider suite continues to test converter failure behavior even when the real corpus has recovered every document.

## API-free acceptance checks

| Case | Expected result |
| --- | --- |
| All sources parsed; 24 valid reviewed rows covering mandatory passage/metadata types | Validation succeeds without a converter row |
| At least one current quarantined source; otherwise valid dataset but no converter row | Validation fails with the missing operational type |
| Same quarantined corpus with a valid unanswerable operational row and its assigned dev family | That missing-type error disappears |
| Converter row refers to a parsed recovery extraction | The shared checker still rejects it |
| A reviewed row has a stale extraction, missing quote, wrong family or the drafter as reviewer | Existing validation errors remain |

Extend the existing tests rather than adding a parallel validation framework. Run `check --phase 2 --provider fake`. Confirm whether this validation-policy change affects frozen run eligibility; if it does, version/invalidate the affected evidence instead of silently reusing it. Never manufacture approval, evidence text or passing metrics to satisfy a gate.

## Then prepare the next gold batch

Read [.wiki/gold-drafting.md](../../.wiki/gold-drafting.md) and the actual rejection record before drafting. `gold submit` requires every rejection to have an inferred cause/lesson/drafting rule and rejects reused question identities. The local operator supplies narrowly scoped source evidence and family assignments; Cloud can draft against those supplied excerpts, while a different person approves against the original.

Use `templates/gold-draft.example.json` as the row shape. Fill IDs from the current inventory and `datasets/families.json`; never choose a family split yourself. Keep `reviewed_by` and `reviewed_at` null in drafts. Ensure the new batch restores at least 24 accepted rows and includes numeric qualifiers, late table evidence and repeated codes. Source quotes belong in local candidate inputs, not the shared baseline inventory.

After that, follow `README.md` for frozen K0/K1 comparisons and use actual failures/results for further changes and selection drafts. Dense, HR measurements and activation remain separately decided local operations.
