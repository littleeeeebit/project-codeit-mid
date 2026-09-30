# Building a difficult golden dataset with LLM assistance

Proposed approach: the LLM drafts questions and evidence-backed answer candidates; a person other than the drafter approves the final gold. Generate only after complete extraction has been checked. The supplied CSV previews and undocumented summaries are unsuitable as the sole gold source.

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
6. Give the question and original to an independent human reviewer. Rewrite leading wording, copied answers in the question, and unnatural requirement-code hints where they make retrieval too easy.
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
| Generator judges itself favorably | Independent human approval; calibrate any automated judge on reviewed examples |
| One near-duplicate appears in both splits | Split by source/document family, not random question rows |
| Negatives are actually answered elsewhere | Validate absence in the permitted corpus and selected scope |
| Synthetic difficulty is irrelevant | Have consultants/team members write realistic information needs first |
| Gold encodes one system's retrieval failures | Label source evidence without using the candidate system's top-k as the authority |

Reserve up to $3 for gold drafting and limited answer evaluation combined; see [the budget plan](budget.md). Draft several candidates from one reviewed passage group, cache the result, and validate locally. Rejecting a bad candidate need not trigger another paid call. Six members can each independently review 20 accepted rows, with a second review for disputed numeric, deadline, and conflict cases.

For a two-day delivery, start with 24 reviewed development questions while the broader set is prepared. Report this as a pilot, not a reliable final benchmark. The sealed set is used only after selecting the pipeline. Small samples need uncertainty intervals and per-type reporting, as described in [evaluation](evaluation.md).
