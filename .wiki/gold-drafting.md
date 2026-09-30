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
