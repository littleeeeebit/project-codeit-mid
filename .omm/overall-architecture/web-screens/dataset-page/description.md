The 데이터셋 만들기 page works in three steps:
1. Pick development documents and passages through `/api/drafting/documents` and `/elements`, and build slots with `/api/drafting/slots`.
2. Price the run with `/api/drafting/plan`, then start it with `/api/drafting/start` and the consented maximum.
3. Send the valid drafts to the queue with `/api/drafting/runs/{id}/submit`.

Reviewers then read `/api/gold/pending` and `/api/gold/{candidate_id}`, and approve or reject with `/api/gold/{candidate_id}/decide`.