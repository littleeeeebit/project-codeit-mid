# Building a difficult golden dataset with LLM assistance

Approach: the LLM drafts questions and evidence-backed answer candidates. An AI reviewer approves the final gold, development and sealed, and its identity differs from the drafter's; a drafter never approves its own row. People do not approve rows one by one: they choose from comparison tables and activate ([operating rule](../plan/end-to-end/0-overview.md#operating-rule-pipelines-run-reviewers-approve-a-person-picks)). Generate only after complete extraction has been checked. The supplied CSV previews and undocumented summaries are unsuitable as the sole gold source.

[Ragas test generation](https://docs.ragas.io/en/stable/concepts/test_data_generation/rag/) distinguishes single-hop, multi-hop, specific, and abstract questions and uses document relationships to generate scenarios. Reuse that scenario taxonomy. A full automated graph generation pipeline is optional here: a curated section or pair of evidence passages is cheaper and easier to audit for 100 RFPs.

## Proposed 120-question set

Create 60 development and 60 sealed test questions, grouping document families before the split. The counts below are targets for the combined set, with the same proportions in both splits where possible.

| Question type | Count | What makes it useful |
| --- | --- | --- |
| Direct fact | 24 | Budget, period, deadline, organization, or submission method with qualifiers |
| Semantic paraphrase | 18 | A realistic user description without the exact source wording |
| Exact identifier | 12 | Requirement code with document scope and repeated codes in distractors |
| Table and numeric interpretation | 18 | Headers, merged cells, KRW units, VAT, dates, or mandatory thresholds |
| Multiple passages in one document | 12 | A rule and its exception, or method plus submission timing |
| Comparison across documents | 12 | Evidence from each selected RFP rather than a single generic answer |
| Missing answer or false premise | 12 | Verified absence, incomplete premise, or a required clarification |
| Version and near-duplicate confusion | 12 | Similar titles, institutions, revisions, or conflicting records |

Most questions must demand more than copying a sentence. Difficulty must still be answerable from the available evidence. Metadata lookups belong in their own evaluation stratum and must not inflate passage-retrieval scores.

## Procedure

1. Cluster byte-identical originals, similar titles, revisions, and overlapping extracted text into document families. Keep related questions and paraphrases in the same split.
2. Assign families to development or test. Both are indexed for ordinary retrieval; test questions and answer labels remain hidden from tuning. This is evaluation separation, not exclusion of test documents from the search corpus.
3. Select reviewed source elements, including late-document requirements and difficult tables. Provide the LLM only those elements with stable source IDs.
4. Ask for a consultant's question, minimal answer claims, exact evidence quotes, source references, and why the question is difficult. Request no question when the chosen passages do not support a meaningful answer.
5. Check that every evidence quote exists in the indicated source version. Validate amounts, dates, units, document identity, and required/optional wording against the original view.
6. Give the question and original to an AI reviewer that did not draft it. Rewrite leading wording, copied answers in the question, and unnatural requirement-code hints where they make retrieval too easy.
7. For negative examples, search the complete permitted corpus before labeling absence. Artificially removing retrieved evidence is a retrieval-failure test, not proof that the original has no answer.
8. Freeze accepted rows with reviewer, evidence provenance, split, generation model/prompt version, and dataset hash. Change labels only through a recorded correction.

## A record contract

```json
{
  "question_id": "example-only",
  "split": "dev",
  "question": "Consultant question in Korean",
  "scope_doc_ids": ["stable-document-id"],
  "as_of_date": "2025-02-01",
  "answerability": "answerable",
  "question_type": "table_numeric",
  "difficulty_reason": "Amount requires its table header and VAT note",
  "required_claims": [{"field": "budget_krw", "value": 130000000}],
  "evidence": [{
    "doc_id": "stable-document-id",
    "source_sha256": "hash-of-reviewed-original",
    "element_id": "stable-source-element-id",
    "quote": "Exact source evidence goes here"
  }],
  "review_status": "pending",
  "reviewer_id": null
}
```

This is a schema illustration, not an accepted gold row. Use `answerable`, `unanswerable`, `ambiguous`, and `conflicting` according to the available originals and permitted scope. Store required evidence alternatives when more than one passage supports the same fact. Do not make mutable chunk IDs the only evidence labels.

## Failure modes and prevention

| Failure | Prevention |
| --- | --- |
| LLM invents a plausible date, document, or reference | Validate exact quoted evidence and inspect the original |
| CSV preview hides the true answer | Generate and label against reviewed full originals |
| Questions copy chunk headings and vocabulary | Add independently written user paraphrases and shorter queries |
| Generator judges itself favorably | Approval by an AI reviewer with a different identity from the drafter; calibrate any automated judge on reviewed examples |
| One near-duplicate appears in both splits | Split by source/document family, not random question rows |
| Negatives are actually answered elsewhere | Validate absence in the permitted corpus and selected scope |
| Synthetic difficulty is irrelevant | Have consultants/team members write realistic information needs first |
| Gold encodes one system's retrieval failures | Label source evidence without using the candidate system's top-k as the authority |

Reserve up to $3 for gold drafting and limited answer evaluation combined; see [the budget plan](budget.md). Draft several candidates from one reviewed passage group, cache the result, and validate locally. Rejecting a bad candidate need not trigger another paid call. AI reviewers review every row, with a second AI review, by an identity that is neither the drafter nor the first reviewer, for disputed numeric, deadline, and conflict cases.

For a two-day delivery, start with 24 reviewed development questions while the broader set is prepared. Report this as a pilot, not a reliable final benchmark. The sealed set is used only after selecting the pipeline. Small samples need uncertainty intervals and per-type reporting, as described in [evaluation](evaluation.md).

## Rejection-guided Luna generation

`gold generate --file <plan.json> --out <absolute-new-private-directory> --max-cost-usd <ceiling>` drafts development rows with `gpt-6-luna`. Configure the OpenAI provider, account and shared budget first, and stop the serving process: drafting holds the same exclusive gateway lock for its entire invocation. It uses the existing transport and `gold_eval` purpose envelope. Five planned topics share each request; there are no hidden retries, automatic submissions or approvals. The cost ceiling covers the entire invocation, including drafts that fail validation.

The private plan has a `slots` list of at most 50 entries. Each entry supplies `question_id`, `revision`, `question_type`, `intent`, `as_of_date`, `scope` (current document/source/extraction IDs), and `sources` (document and element IDs). Resolve the original locations and inspect the full conditions when preparing those sources. The generator resolves text from the database and derives families and mode; development drafting refuses sealed, stale, missing or overlapping source families. It currently generates answerable passage questions, including explicitly contradicted false premises. Verified absence and revision conflicts still need a separate complete-original review.

The owner CLI can add `--dataset test` to prepare pending sealed candidates from families already assigned to test. Its output must be inside the configured data directory's `sealed/` area, and in-process callers need the `sealed_evaluator` capability. Development rejection lessons may inform the fixed drafting prompt; sealed rejections never enter development learning. An AI reviewer with a separate identity reviews those private candidates through the owner CLI (`gold show`, `gold decide --reviewer <identity> --original-inspected`) before they are frozen. The person selects the serving configuration from the development comparison tables, and `freeze-release` precedes the first sealed answer run. Drafting does not grant sealed execution or approval.

Before every invocation it reads all development rejection pages, checks their database projections, and requires an analyzed cause, lesson and imperative drafting rule. These analyses accompany the scoped sources and synthetic contrastive examples in the prompt. The prompt distinguishes the actual seven task types, asks for atomic claims and short complete evidence spans, and warns against heading lookups, omitted exceptions, header-only citations and transferring conditions across documents. Sealed rejection pages and accepted evaluation labels are excluded from the prompt.

The output directory contains the plan, rejection analysis, serialized prompts, raw responses, response IDs, usage, latency, settlements, valid pending `candidates.jsonl` and `invalid.json`. Tokens and reservation costs are measured after the final prompt, examples, lessons and sources have been assembled. Billing settles before output validation, so invalid output remains charged and traceable. An uncertain attempt stops further drafting until reconciled; an existing output directory cannot be overwritten.

The response uses a strict schema, including nested qualifier arrays. Each qualifier list contains one exact spelling: different mandatory conditions are separate requirements, not alternatives. Numeric tasks need a typed numeric/date claim. A dated submission cutoff also preserves its stated `time` as `HH:MM`; a deadline draft quoting a time but omitting that typed field is refused. Sources carry explicit document/institution identities to prevent reversed comparison answers. Quote anchoring tolerates whitespace differences only, requires a unique match, and stores the untouched source slice and offsets alongside the generator's original quote. A changed word, number or ambiguous repeated match remains invalid. Cached raw output can be revalidated locally after an anchoring change, without another paid call.

The generator can also select a precomputed `span_id` instead of copying a quote. The host binds that selection to the original offsets; repeated phrases stay distinguishable. The splitter recognizes PDF bullets, HWP's private-use bullet and nested list items. Parent actor/timing conditions remain separate spans and must be included in the claim's support. Ordered spans carry the entire source once; the prompt omits its duplicate full-text field and coalesces identical rejection rules while retaining every candidate ID. Keep questions focused, but do not discard a required condition merely to hit a claim-count limit. Reviewer corrections to source labels must preserve the raw API response and record changed fields and the original-source reason.

Review each valid candidate against its original before `gold submit` and `gold decide`. The drafter is `api-gpt-6-luna`; the reviewing agent records its actual separate identity and automated-review scope. A drafter can withdraw its own candidate by rejecting it with a concrete note, but cannot approve it. Keep manual label changes traceable, and send substantive defects back through rejection analysis and a new prompt/revision rather than hiding them in an edited batch. Measure failure categories and first-pass acceptance again after the last prompt or validator change. Development approval does not approve sealed rows: they get their own AI review through the owner CLI.

The verifier's reviewed-question selector includes eligible `dev` gold-2 rows and the older `dev-pilot` rows. It uses each selected gold row's scoped documents and reference date; pending, stale or unresolved disputed rows and sealed questions are excluded.
