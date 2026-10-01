# Release report F-126bacca0d

Generated 2026-10-01T14:14:30.164628+00:00 from `data/` state. Every figure is read from recorded evidence; anything not run is listed as missing. This command never generates an answer.

## Decision: **limited**

- hard check not verified: automated invariants (check --phase all --provider fake) (no saved run: check --phase all --provider fake --save)
- quality target not measured: multi-evidence complete coverage@20 >= 0.80 ({'numerator': 0, 'denominator': 0, 'rate': None, 'wilson95': None})
- quality target not measured: negative/ambiguous handling >= 0.90 ({'numerator': 0, 'denominator': 0, 'rate': None, 'wilson95': None})
- quality target not measured: full-answer p95 < 15 s at six users (real provider) (no real latency sample)
- evidence is sealed pilot, not a sealed evaluation of reviewed gold

## Release manifest

- code: Git `76104d1fd52c1693c2b24bfbf19ed8861fe11b60` (uncommitted changes: False), package source `4a73fd972fb7314e`; metric code `fa53570261d80c6f`
- model gpt-6-luna, reasoning low, output cap 2000, prompt grounded-answer-3, rates openai-standard-gpt-6-luna-2026-09-30, settings `a9152d1ca446276c`
- serving: run `K1-a40883b509` (kiwi_bm25), keyword index `79debd8b66f91dc9`, dense `None`, reranker None; fallback kiwi_bm25
- dependencies: requirements-lock.txt `060fd9cdbdc949a3`; hardware {'platform': 'Linux-6.18.44-fc-v50-x86_64-with-glibc2.39', 'machine': 'x86_64', 'python': '3.12.3', 'cpu_count': 4}
- freeze: {'freeze_id': 'F-126bacca0d', 'frozen_at': '2026-10-01T14:14:01.928077+00:00', 'selected_run_id': 'K1-a40883b509', 'decided_by': 'owner', 'post_test': False}

## Coverage

- associations 5 (1 HWP, 4 PDF), unique sources 4; audit differences {'associations': (5, 100), 'hwp': (1, 96)}
- parse {'parsed': 4, 'quarantined': 1}; review {'sample_checked': 4, 'needs_recovery': 1}; human-checked sources 3 (automatic verdicts are not counted as review)
- quarantined: ['기관E_재난 관리 시스템.hwp']
- byte-identical associations with conflicting metadata: 2

## Datasets

- `dev`: 4 rows, valid True (0 errors), label **pilot**, targets met 0/8, metadata stratum 0, positions {'early': 0, 'middle': 1, 'late': 2, 'none': 1}; frozen `51734545cbe5` (current True)
- `test`: 1 rows, valid True (0 errors), label **pilot**, targets met 0/8, metadata stratum 0, positions {'early': 0, 'middle': 0, 'late': 1, 'none': 0}; frozen `47c1d87073f2` (current True)

## Retrieval runs (development, retrieval only)

| Run | Mode | Scored | hit@20 single | complete@20 multi | nDCG@5 | MRR | Critical | Wrong scope | p95 ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `K0-480d0206c9` | whitespace_bm25 | 3 | 0.5 (1/2) | 0.0 | 0.5377 | 0.6667 | ['dev-amount', 'dev-deadline'] | 0 | 0.37 |
| `K1-a40883b509` | kiwi_bm25 | 3 | 1.0 (2/2) | 1.0 | 1.0 | 1.0 | [] | 0 | 1704.7 |

## Answer evaluation

- development run `A-3e0ab6e52116` (complete): see `runs/A-3e0ab6e52116/report.md`
  - `K1-a40883b509` (kiwi_bm25) 4/4: claims 0.6667 (n=3), critical wrong 0, citation precision lower bound 1.0, negatives 0.0 (n=1), cost $0.000385
  - `K0-480d0206c9` (whitespace_bm25) 4/4: claims 0.3333 (n=3), critical wrong 0, citation precision lower bound 1.0, negatives 1.0 (n=1), cost $0.000187
- sealed run `S-d2784be48a71` (sealed test, complete) under freeze `F-126bacca0d`

## Latency

- fake sample `L-6eda5c26c78e`: n=12, failures 0, warm p50/p95 101.0/135.78 ms (n=6), cold first wave 176.3 ms, 6 users × 2 waves

## Checks

- no saved check results (`check --phase all --provider fake --save`)

## Hard checks and quality targets

| Check | Result | Evidence |
| --- | --- | --- |
| automated invariants (check --phase all --provider fake) | unverified | no saved run: check --phase all --provider fake --save |
| no critical wrong deadline/amount/mandatory condition/institution in reviewed release cases | pass | 0 observed wrong, 0 contested or awaiting blind review |
| no selected-scope leakage | pass | claims/evidence outside scope 0, wrong-scope candidates 0 |
| managed evidence links resolve | pass | {'numerator': 1, 'denominator': 1, 'rate': 1.0, 'wilson95': [0.2065, 1.0]} |
| admission cap enforced and ledger not frozen | pass | committed $0.001805 of cap $5.000000; frozen None |
| no unresolved billing | pass | 0 open or unknown attempts |
| single-evidence hit@20 >= 0.90 | met | {'numerator': 1, 'denominator': 1, 'rate': 1.0, 'wilson95': [0.2065, 1.0]} |
| multi-evidence complete coverage@20 >= 0.80 | not measured | {'numerator': 0, 'denominator': 0, 'rate': None, 'wilson95': None} |
| required-claim correctness >= 0.90 | met | {'numerator': 1, 'denominator': 1, 'rate': 1.0, 'wilson95': [0.2065, 1.0]} |
| citation precision >= 0.95 (unjudged links count as unsupported) | met | {'numerator': 1, 'denominator': 1, 'rate': 1.0, 'wilson95': [0.2065, 1.0]} |
| negative/ambiguous handling >= 0.90 | not measured | {'numerator': 0, 'denominator': 0, 'rate': None, 'wilson95': None} |
| full-answer p95 < 15 s at six users (real provider) | not measured | no real latency sample |

## Budget

- spent $0.001805 of the $5.000000 allowance (0.04%); pending $0.000000 (unknown $0.000000); cap $5.000000; paid enabled True; last reconciliation never; dates 2026-09-30..2026-10-28
- envelopes (used incl. open / envelope): embedding $0.000000/$0.312500, gold_eval $0.001805/$0.937500, interactive $0.000000/$3.750000
- adjustments: [('prior-use-baseline', 0)]
- tracked scope: calls through this application's gateway plus recorded adjustments; not provider credit

## Mentor walkthrough

- not recorded for this candidate: follow docs/operations/runbook.md §10.6 and save `releases/F-126bacca0d/walkthrough-results.json`

## Restore drills

- 2026-10-01T14:14:29.845699+00:00: passed — backup `backup-1`, spent $0.001805, pending $0.000000, unknown $0.000000, prior use $0.000000
