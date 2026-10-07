The dataset builder (데이터셋 만들기).
- draft.tsx: loads GET /api/drafting/documents (development-family documents only) and their elements, creates slots with POST /api/drafting/slots, gets a free maximum-cost estimate from POST /plan, starts with POST /start carrying `consented_max_micro_usd`, and sends a finished run's valid drafts to the queue with POST /runs/{id}/submit.
- dataset-page.tsx: reads GET /api/gold/pending and /api/drafting/runs.
- review.tsx: shows GET /api/gold/{id} next to its source spans, and posts /decide with `expected_sha`, a required note and reject categories.