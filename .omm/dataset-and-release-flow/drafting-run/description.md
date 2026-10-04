The draft UI (`web/src/components/dataset/draft.tsx`) calls these routes in order:
1. `GET /api/drafting/documents` (dev-family parsed documents only, via `evaluation.GoldChecker`).
2. `GET .../{doc_id}/elements?contains=` to pick source passages.
3. `POST /api/drafting/slots` (`service.draft_slot` validates type, intent, one or two documents, and a source per document).
4. `POST /api/drafting/plan`, which is free: it prices five slots per call at full output and reports whether the plan `fits` the `gold_eval` envelope and the cap.
5. `POST /api/drafting/start` with `consented_max_micro_usd`.

After the run, `POST /api/drafting/runs/{id}/submit` → `submit_drafts` refuses unless the run is completed, has rows and was not yet submitted. It runs `evaluation.assign_families`, then `gold.submit(candidates.jsonl, batch=run_id, dataset='dev', drafted_by='api-gpt-6-luna')`. Agents can also submit through `cli gold submit` (procedure in `.wiki/gold-drafting.md`).