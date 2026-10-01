---
scope: project
severity: rule
triggers: ["골든|골드|데이터셋|질문 초안|초안 질문|거절|파일럿|gold|dataset|pilot|candidate"]
domain: rag
reads:
  - docs/rag/golden-dataset.md
  - src/rfp_assistant/gold.py
---

# Draft dataset questions only after learning from the rejection wiki

Rule. Before drafting any dataset candidate, read every rejection in the rejection wiki and record an inferred reason for each one. Then apply every learned drafting rule to the new batch. `gold submit` refuses a batch while any rejection still lacks an inferred reason. It also refuses a question already drafted for the same document, whether that question was approved, rejected or is still pending.

## Where everything is

The rejection wiki lives next to the dataset, outside git, because its pages quote RFP originals.

| What | Path (repository-relative) |
| --- | --- |
| Rejection wiki index | `.runtime/datasets/rejections/index.md` |
| One page per rejection | `.runtime/datasets/rejections/<candidate_id>.md` |
| Every batch as drafted, immutable | `.runtime/datasets/candidates/<batch_id>.jsonl` |
| Approved dataset (approved rows only) | `.runtime/datasets/<dataset>.jsonl`, e.g. `dev-pilot.jsonl` |

On this machine the index is `C:\Users\dasdk\PycharmProjects\project-codeit-mid\.runtime\datasets\rejections\index.md`. When `RFP_DATA_DIR` is set, replace `.runtime` with that directory. `python -m rfp_assistant.cli gold status` prints the resolved index path and the candidate IDs still waiting for an inferred reason.

Every rejection page has the same structure:
1. Frontmatter holds the candidate, batch and dataset IDs, the row and batch hashes, the drafter, the reviewer and time, the reviewer's category codes, and the inference status.
2. The body has these sections, in order: Question, Reviewer decision (categories and note), Inferred reason, Candidate row (verbatim JSON), and Source context at rejection. The last section shows the document, the full text of each cited element and the CSV fields.

## Procedure

1. Run `python -m rfp_assistant.cli gold status` and open the index.
2. For each ID under "Pending inference", open its page. Compare the verbatim row with the source context and the reviewer's categories and note. Work out why the reviewer rejected it.
3. Write a JSON file `{"cause": ..., "lesson": ..., "drafting_rule": ...}` and record it with `python -m rfp_assistant.cli gold infer --candidate-id <id> --by <agent identity> --file <path>`.
   - `cause` is what was wrong in this row.
   - `lesson` is the general mistake behind it.
   - `drafting_rule` is one imperative sentence the next batch must follow.
4. Read "Drafting rules learned" in the index and apply every rule while drafting.
   - Draft rows with the same fields as the verbatim rows, from source families assigned to `dev` in `.runtime/datasets/families.json`.
   - Each quote must appear in the cited element.
5. Submit with `python -m rfp_assistant.cli gold submit --file <batch.jsonl> --batch <new-batch-id> --dataset dev-pilot --drafted-by <agent identity>`.
6. Run `python -m rfp_assistant.cli gold check`. It must report `ok`.

## Consistency

The `gold_candidates` table in `.runtime/rfp.sqlite3` is the only authority. The dataset file, the rejection pages and the index are rendered from it in the same transaction as each decision. Never edit them by hand: `gold check` compares every file byte for byte with its rendering and fails on any difference, and `gold sync` rewrites them from the database. Reviewers approve or reject on the `질문 검토` screen; a drafter cannot review its own rows.

## Phase 4 gold rows (`dev` and the sealed `test`)

The phase-4 datasets use schema `gold-2` (`docs/plan/end-to-end/4-evaluation-and-release.md`, "Dataset contract and split"). The queue, rejection wiki and `gold infer` loop are the same; the row shape and the sealed path differ.

- **Shape.** Each row has `dataset_version: "gold-2"`, a stable `question_id` and `revision` (a correction is a new candidate with a higher revision of the same ID, never an edit), `split`, `question`, `mode` (`single`, `compare` or `metadata`), `scope` (one or two `{doc_id, source_hash, extraction_id}`), `as_of_date`, `family_ids` (exactly the scoped documents' assigned families), `question_type`, `difficulty_reason`, `answerability`, `expected_status`, `required_claims`, `evidence_groups`, `negative_validation`, `review` and `generation_provenance`. `tests/phase4_fixtures.py` builds valid examples.
- **Evidence.** One evidence group per required fact or condition, each with alternative source spans: `source_hash`, `extraction_id`, `element_id`, the exact `quote`, and optionally raw `offsets` or table `cells`. Never a chunk ID. A requirement and its nearby exception (an amount and its VAT note) are separate groups unless one span holds both.
- **Claims.** Each required claim is typed: `number` (integer `value` plus `unit`), `date` (ISO `value`, optional `time`) or `text` (`patterns`). It lists its `support_groups`, `qualifiers` (each a list of permitted spellings, one of which must appear in the support quotes), and `criticality` with a `critical_kind` (`deadline`, `amount`, `mandatory_condition`, `institution`) when critical. The validator checks that the value is actually stated in its support quotes.
- **Negatives.** `unanswerable` and `ambiguous` rows need `negative_validation`: every scoped document searched, the methods, the original locations inspected, `original_complete: true` and a rationale. A quarantined or unparsed source is never source absence; it belongs to the operational suite, not gold.
- **Drafts.** Keep `review.reviewed_by`, `approved_at` null and `status: "pending"`. `review.drafted_by` must equal `--drafted-by`. An LLM draft records `generation_provenance` (`model`, `prompt_version`, `source_set_hash`); the drafting model can never approve.
- **Development rows** (`--dataset dev`) are reviewed on the `질문 검토` screen. Approval requires ticking that the original was inspected; a reviewer can mark a row disputed, and a third person then records a second review in "2차 검토 대기".
- **Sealed rows** (`--dataset test`) are drafted only from families assigned to `test`. Their batch files, dataset and rejection pages live under `.runtime/sealed/`, and the review screen never lists them. The owner shows and decides them in the terminal: `gold show --candidate-id <id>`, then `gold decide --candidate-id <id> --decision approve --reviewer <name> --original-inspected` (or `--decision reject --category <code> --note ...`), and `gold second-review --candidate-id <id> --reviewer <name> --agree|--disagree --note ...`. `gold excerpts` never includes sealed rows.
- **Freeze.** `validate-gold --dataset dev|test` must report `ok`. Then `freeze-dataset --dataset dev|test --actor <name> --reason <why>` records the dataset, review-log and family-map hashes. A set below the per-type targets is labeled `pilot`, never `gold`.
