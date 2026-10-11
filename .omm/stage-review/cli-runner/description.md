`stage_review.run(stage, base, cands)`:
1. Resolves refs ('.' means the working tree with its changed and untracked files).
2. Freezes inputs: the serving activation, index, vectors and questions; or reuses another run's inputs via `--inputs-from`.
3. Copies the worker into the run folder and runs each candidate.
4. Paid stages stop with `state: needs_approval`.
Free stages (retriever, chunking) are scored by one scorer into view.json. `stage-review run traffic` instead freezes real 질문하기 requests from a read-only session (default_transaction_read_only) with HMAC-pseudonymized member IDs, per README.