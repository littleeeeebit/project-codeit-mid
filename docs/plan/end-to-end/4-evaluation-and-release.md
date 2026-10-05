# Phase 4 — difficult gold, frozen evaluation and release handoff

Goal: demonstrate the implemented end-to-end system with reviewed source evidence, fair retrieval/answer comparisons, operational checks and honest quality/cost limits. Freeze the selected configuration before sealed evaluation, then hand off a reproducible release and recovery procedure.

Expected window: day 2–3. Gold preparation starts in phase 1 and continues during phases 2–3; this phase completes review and evaluation rather than beginning them. The target is 120 reviewed questions, but a smaller independently reviewed pilot must be labeled as such when the deadline wins.

## Entry and files

Read [shared contracts](implementation-contracts.md), [gold research](../../rag/golden-dataset.md), [evaluation research](../../rag/evaluation.md), [delivery research](../../rag/delivery-plan.md) and the actual phase-1/2/3 reports. Inputs are immutable source/index/config manifests, reviewed coverage, development pilot, family assignments, retrieval finalists and available evaluation allowance.

Expand `evaluation.py`, `cli.py`, the verifier actions and focused `tests/test_evaluation.py`. Tests run on isolated PostgreSQL databases that `tests/fixtures.py` creates per test environment on the server started by `tools/start-postgresql.ps1`, or on the server named by `RFP_POSTGRES_TEST_DSN`; they never touch the application database. Reuse existing trace/gateway records. Write `docs/operations/runbook.md` and `docs/operations/release-report.md` with actual outcomes; sanitized runtime manifests/reports may be copied or linked as appropriate. Do not put private tokens, the academy key, sealed labels or an entire mutable runtime database into Git.

## Dataset contract and split

Extend the research record into a validated JSONL schema with these fields:

| Field | Requirement |
| --- | --- |
| `question_id`, `dataset_version`, `split` | Stable ID, version/hash and `dev` or `test`; corrections append a new revision |
| `question`, `mode`, `scope`, `as_of_date` | Realistic Korean wording, explicit selected doc/source versions and permissible date |
| `family_ids`, `question_type`, `difficulty_reason` | Source-family membership and declared stratum; all comparison families belong to the same split |
| `answerability`, `expected_status` | `answerable`, `unanswerable`, `ambiguous` or `conflicting`; technical ingestion-unavailable cases are a separate operational suite |
| `required_claims` | Typed fact/value/unit/qualifier, criticality, permitted alternatives and matching rules |
| `evidence_groups` | One group per required fact/condition, with alternative valid source spans/cells and exact quotes |
| `negative_validation` | Scope searched, reviewer methods/locations, original completeness and rationale; required for verified absence |
| `review` | Drafter/generator, independent reviewer, original inspection, approval time and disputed second review |
| `generation_provenance` | Human or draft model/prompt/source-set hash, paid attempt IDs and candidate edits |

Question IDs and evidence labels must not depend on current chunk IDs. Evidence includes source hash, extraction revision, element ID, raw offsets/cells and quote. A requirement with a nearby exception has separate evidence groups unless one stable span contains both. Alternative spans support the same fact; retrieving two alternatives does not recover two facts.

Split families before drafting. Group byte-identical sources, revisions and strongly overlapping related originals through a reviewed family map. Keep related paraphrases/questions together. Draft cross-document comparisons from families already in the same split; do not connect a dev family to a test family afterward. All originals remain in the searchable corpus; only test questions/labels are sealed.

| Type | Target total | Dev | Test |
| --- | --- | --- | --- |
| Direct fact | 24 | 12 | 12 |
| Semantic paraphrase | 18 | 9 | 9 |
| Exact identifier | 12 | 6 | 6 |
| Table/numeric | 18 | 9 | 9 |
| Multiple passages in one document | 12 | 6 | 6 |
| Cross-document comparison | 12 | 6 | 6 |
| Missing/false premise/clarification | 12 | 6 | 6 |
| Revision/duplicate conflict | 12 | 6 | 6 |
| Total | 120 | 60 | 60 |

Metadata-only direct questions have their own stratum and are excluded from passage retrieval denominators. Add converter failures, interrupted calls and request-ownership races to a separate operational suite; do not inflate source-unanswerability scores with them.

## Ordered work

### 1. Draft affordably from reviewed original evidence

1. Validate family assignments and available review coverage. Use late sections, hard table headers/units, exceptions, repeated codes and misleading metadata, not just introduction paragraphs.
2. Have team members write realistic consultant information needs first. Optional LLM drafting receives a small curated source group with stable evidence IDs and asks for candidate question, minimal required claims, exact quotes and difficulty rationale. It may return no candidate.
3. Batch several candidates per source group and cache by source/prompt/model hash. Use the shared gateway and combined $3 gold/evaluation envelope. A rejected candidate does not automatically trigger another call.
4. Locally validate schema, quote existence, scope/version identity and numeric/qualifier consistency. Candidate status stays pending until an AI reviewer, whose identity differs from the drafter's, checks it against the original. The drafter and the drafting model cannot approve their own rows.
5. Do not transmit the full RFP or create a separate knowledge graph just to generate questions. Manual drafting is the zero-cost fallback and often the better source of natural questions.

### 2. Independently review and freeze labels

AI reviewers approve or reject every row, development and sealed, under the [operating rule](0-overview.md#operating-rule-pipelines-run-reviewers-approve-a-person-picks). Each review records the reviewer's identity, which must differ from the drafter's and from the drafting model's. Reviewers see the original, question and proposed claims; they do not use the selected retriever's top-k as the authority. For disputed deadlines, institutions, amounts, mandatory conditions and conflicts, a second AI reviewer, differing from the drafter and the first reviewer, records the second check. Sealed rows are reviewed through the owner CLI (`gold show`, `gold decide`, `gold second-review`), so their text never reaches the verifier pages. No person approves rows one by one.

Check that the question does not supply its answer, borrow an unnatural unique heading, or depend on outside knowledge. Validate amount units/VAT, date type/timezone, mandatory/optional language, scope and every required condition. Rewrite unrealistic questions while preserving source provenance.

For negatives, search and inspect the complete permitted originals, not merely the extracted top-k. Record how absence was verified and whether ingestion is complete enough to establish it. Ambiguity/conflict is labeled separately. Source evidence omitted by a parser is a technical failure, not an absent source fact.

`validate-gold` rejects pending/unreviewed accepted rows, nonexistent quotes, cross-split families, duplicate/paraphrase leakage, mismatched hashes, chunk-only labels and missing negative validation. It reports per-type/source-position counts and rejected rows. Freeze dev/test manifests and review log hashes. Store sealed data outside ordinary verifier access; the service and filesystem owner boundary both enforce it.

### 3. Freeze experiment inputs and metric definitions

Every run captures Git revision if available plus uncommitted source fingerprint, dataset/review version, source/extraction/index hash, analyzer/dictionary/chunker, retrieval/reranker/model/prompt config, token limits, rates, hardware and time. If the repository has not been committed, say so; never fabricate a clean revision.

Canonical relevance is mapped from returned raw spans/cells to required evidence groups. Headers and qualifiers are part of the label. Do not match solely on a requirement code or document title. Deduplicate overlapping passages that repeat the same supporting span before ranking metrics; evidence coverage counts each group once.

| Metric | Definition to implement |
| --- | --- |
| Hit@20 | Answerable single-group queries with at least one fully supporting returned unit divided by eligible single-group queries |
| Evidence recall@20 | Retrieved required evidence groups divided by all required groups, averaged per eligible query; alternatives satisfy one group |
| Complete coverage@20 | Eligible multi-group queries with all groups recovered divided by eligible multi-group queries |
| nDCG@5 | `DCG = sum((2**grade - 1)/log2(rank+1))` over deduplicated judged units, divided by ideal DCG for the frozen judged pool; define grade 2 as complete support, 1 as useful partial support, 0 as irrelevant |
| MRR | Mean reciprocal rank of the first fully supporting unit on declared eligible queries; misses contribute zero |
| Wrong scope/version | Retrieved and packed units outside captured scope; report count/rate and every example |
| Required-claim correctness | Correct reviewed required claims divided by expected required claims; preserve units/conditions and report question-level completeness too |
| Unsupported claim rate | Unsupported material generated claims divided by all material generated claims; independent support review |
| Citation precision/coverage | Supporting claim-citation links divided by reviewed links; separately, material claims with valid support divided by material claims |
| Negative handling | Correct expected status/action among declared negative/ambiguous/conflict cases; report false answers and unnecessary refusal separately |
| Link validity | Authorized evidence targets resolving to the pinned original/extraction; a valid link does not establish semantic support |
| Runtime and cost | p50/p95 by warm/cold/concurrency condition; per-attempt and per-question USD, with retries/judging/indexing reported separately |

For nDCG, create a judged candidate pool from development runs using original source support, freeze relevance/aggregation rules and record pool coverage. Profile-dependent candidates are mapped to stable evidence; new unjudged passages are not silently assumed irrelevant. Review them blind before finalizing a development comparison. On sealed evaluation, use the predeclared source-span grading and independent assessment procedure without tuning the system. Never compare numbers whose pool/denominators differ without saying so.

An empty eligible denominator is reported as not applicable, not 100%. Report micro/macro aggregation explicitly. Keep retrieval-before-pack and packed-context metrics separate. A partial table fact without its required header/exception does not get complete support credit.

### 4. Run retrieval-first comparisons and at most two answer finalists

Reuse phase-2 frozen K0/K1/D/H/HR ranks where their dataset/source/config hashes match. On expanded development rows, rerun retrieval-only using cached query embeddings: `compare --matrix <name>` reruns a whole matrix in one command and reuses every cached index, vector and score. Compare chunking and reranking as isolated changes; candidate recall and exact-code/scope failures remain visible.

The person selects at most two development finalists from the comparison tables. Keep generation model, maintained prompt, output cap and evidence ceiling fixed for the retrieval-to-answer comparison. `plan-run` estimates all uncached embedding, generation and optional judge attempts before executing. An estimate/config mismatch requires replanning; a budget-blocked run remains partial with status, not secretly reallocated.

Review numbers/critical facts deterministically where trustworthy typed labels permit it and independently where context matters. Do not use string similarity as the sole answer score. Optional LLM judging is blind to run names, sampled and calibrated against the reviewed reference verdicts; record agreement/disputes and its own USD. No paid judge fan-out across every configuration by default.

Model or prompt comparisons are separate development experiments. If testing `gpt-4.1-mini` or a prompt variant, hold retrieval/evidence fixed and label that factor. No silent automatic escalation. Finalist selection records benefit, critical regressions, latency and cost before sealed access.

### 5. Run the sealed evaluation once after selection

1. Freeze release-candidate config, code/source fingerprint, dataset/review hashes and metric code. The owner, running the sealed job from the CLI (no login exists; the `sealed_evaluator` capability is the job's declared role), verifies freeze completeness and an estimated run budget.
2. Start one recorded sealed run for the chosen candidate. Persist each completed question/attempt. On interruption, resume only unfinished rows with conclusively known billing status; unknown paid attempts need reconciliation, not automatic replay.
3. Capture original trace/output, independently reviewed scores and failure categories. Do not edit prompts/retrieval settings from sealed answers and call the resulting rerun an untouched test.
4. If a discovered critical bug requires a repair, preserve the first result, label any subsequent run as post-test regression, and plan a fresh independently sealed set for a new reliability claim.
5. Report per-stratum sample counts and binary Wilson intervals; use family-grouped bootstrap intervals for ranked metrics when practical. Record the seed and resampling unit. Small strata cannot support a strong leaderboard conclusion.

### 6. Complete integrated release checks

Run `check --phase all --provider fake` across the focused invariants. Reuse already-passing checks unless the final change touched them; the final report records exactly what was rerun. Verify ledger recovery/reconciliation, role isolation, original access, exact codes and source/version identity on the final candidate.

Repeat the phase-3 live consultant/verifier walkthrough on that candidate. Measure warm full-answer latency under six users with an explicit bounded real test if budget permits: at least five waves of six requests gives 30 samples; record n, failures, cold-load observations and per-wave timing. This is a preliminary latency sample, not an SLA. If only fake load or fewer real calls were run, label their limits and do not present them as real model latency.

Use real original facts beyond introductory pages, the duplicated/conflicting records, missing metadata, one unsupported source, two-document comparison, budget cap and changed scope. Fake transport handles destructive/race/exhaustion testing in a temporary ledger on an isolated test database; never drain the shared academy balance to demonstrate a cap.

### 7. Publish the release evidence and operator handoff

Before handoff, implement the minimal owner `backup` and paid-disabled staging `restore-check` commands described in [phase 5](5-team-operation.md), using a gateway-locked PostgreSQL custom-format dump and immutable-artifact manifest, restored into a distinct empty database named by `RFP_RESTORE_DATABASE_DSN`. Demonstrate one restore preserving settled, prior-use and pending amounts. Phase 5 rehearses and extends ongoing operation; the release runbook must not point to nonexistent recovery commands.

| Artifact | Required contents |
| --- | --- |
| `release manifest` | Code/config/index/dataset/rate hashes, pinned dependencies/model revision, hardware and release state |
| `coverage report` | All 100 associations, parsed/reviewed/quarantined counts, review scope, recovery findings and metadata conflicts |
| `evaluation report` | Actual run matrix, metric definitions/denominators/intervals, original failure examples, limitations and paid totals |
| `budget report` | Prior-use baseline, local settled/pending amounts, external adjustments, project dates/envelopes and reconciliation freshness |
| `runbook` | Setup, API-key placement, exact shared launch and network exposure (no login), index activation/rollback, backup/restore, unknown-attempt recovery and shutdown |
| `mentor walkthrough` | Search → select → question → claim/evidence → original → shared cost; separate verifier trace/comparison and failure-state example |

Keep sanitized shareable reports in `docs/operations/` and runtime details in the common artifact tree. Screenshots and metrics must come from actual runs. README links the overview, phase outcomes/runbook, source limitations and the currently active retrieval mode.

## Planned command surface

```powershell
.\.venv\Scripts\python.exe -m rfp_assistant.cli validate-gold --dataset dev
.\.venv\Scripts\python.exe -m rfp_assistant.cli evaluate-retrieval --dataset dev --runs K0,K1,D,H
.\.venv\Scripts\python.exe -m rfp_assistant.cli plan-run --dataset dev --action answer-finalists
.\.venv\Scripts\python.exe -m rfp_assistant.cli check --phase all --provider fake
.\.venv\Scripts\python.exe -m rfp_assistant.cli release-report --latest
```

Implement controlled verifier actions and equivalent owner CLI commands for paid execution using explicit estimate/run IDs. Sealed execution requires its privileged freeze action; a convenient `--split test` argument cannot bypass it. All paid CLI work uses exclusive maintenance mode and the same ledger. `release-report` reads completed evidence and cannot generate answers while formatting a report.

## Metric/check regression fixtures

| Fixture | Expected result |
| --- | --- |
| Two required groups, only one recovered | Recall 0.5, complete coverage false; not a single-hit success |
| Two alternate spans support one fact | One recovered group, no duplicate recall credit |
| Retrieved amount without required VAT note | Incomplete support; cannot pass the claim's qualifier check |
| Duplicate relevant overlapping chunks | One independent evidence contribution; unchanged ground truth after rechunking |
| Known grades `[2, 0, 1]` and ideal `[2, 1, 0]` | nDCG matches hand-calculated formula; first full support has reciprocal rank 1 |
| No eligible passage cases | Metric not applicable; metadata/negatives reported separately |
| Same original hash in dev and test families | Validator refuses split leakage |
| Pending/self-approved candidate or fabricated quote | Validator refuses accepted gold status |
| Scoped source failure labeled unanswerable | Reject as source-absence gold; move to operational suite |
| Interrupted paid run with partial completed rows | Preserve completed results; no duplicate dispatch/cost on safe resume |

## Release decision and exit

Hard checks: source/evidence ownership, no observed critical wrong deadline/amount/mandatory condition/institution in reviewed release cases, exactly-once billing, enforced admission caps, authorized interfaces and safe source access. Repair failures before enabling the affected paid answer route. A blocked route may retain free discovery with an explicit limitation; it is not a completed full release.

Quality targets: single-evidence hit@20 ≥ 90%, multi-evidence complete coverage@20 ≥ 80%, required-claim correctness ≥ 90%, citation precision ≥ 95%, negative handling ≥ 90%; warm retrieval p95 < 2 seconds and full answer p95 < 15 seconds at six users are hardware-dependent targets. Report actual denominators and uncertainty. Missing a target requires a documented scope/fallback decision, not a claim that it passed.

Mark the release `ready`, `limited`, or `blocked`, with exact reason. `limited` can mean reviewed-subset/pilot-only evidence or optional dense/reranker bypass while the supported complete user path works and hard checks pass. An unreviewed 120-row draft is never final gold. If only two days are available, ship the working supported path with its reviewed pilot, unresolved coverage and next review tasks.

Exit when the consultant and verifier can complete the original-to-answer-to-source-to-cost workflow, the final checks/outcomes are recorded, and another member can launch and recover it from the runbook without undocumented knowledge. Continue shared use under [phase 5](5-team-operation.md); remaining optional improvements cannot invalidate the delivered baseline.
