# Answer run A-e15212d8c13e (development finalists)

- dataset `dev` (`b5e2df5ef7c0`), population `dcb6cd446227`, status **complete**
- model gpt-6-luna (reasoning low), prompt grounded-answer-3, output cap 2000; code `796c4e176b94` dirty=False
- blind reviews applied: 0

## Finalist `K1-f5b26e4852` (kiwi_bm25): 4/4 rows

| Metric | Value |
| --- | --- |
| required-claim correctness | 0.6667 (2/3, Wilson [0.2077, 0.9385]) |
| question-level completeness | 0.6667 (2/3, Wilson [0.2077, 0.9385]) |
| claims needing review | 0 |
| critical wrong values | none observed |
| critical claims contested or awaiting review | none |
| citation precision (judged links) | 1.0 (4/4, Wilson [0.5101, 1.0]) |
| citation precision (lower bound, unjudged = unsupported) | 1.0 (4/4, Wilson [0.5101, 1.0]) |
| unjudged links | 0 |
| citation coverage of material claims | 1.0 (4/4, Wilson [0.5101, 1.0]) |
| unsupported claim rate (reviewed) | 0.0 (0/4, Wilson [0.0, 0.4899]) |
| link validity | 1.0 (4/4, Wilson [0.5101, 1.0]) |
| negative/ambiguous handling | 0.0 (0/1, Wilson [0.0, 0.7935]) |
| false answers on negatives | ['dev-absent'] |
| unnecessary refusals | 0.0 (0/3, Wilson [0.0, 0.5615]) |
| metadata stratum | n/a (0 eligible) |
| technical outcomes | none |
| scope leaks | 0 |
| settled cost (ledger, every attempt) | $0.000386; earlier attempts $0.000000, pending $0.000000, unknown $0.000000, reconciled attempts 0, retried rows 0 |
| latency p50/p95 ms | 21.15/24.46 (n=4, sequential, single process, one call at a time) |

## Finalist `K0-9abbc718ff` (whitespace_bm25): 4/4 rows

| Metric | Value |
| --- | --- |
| required-claim correctness | 0.3333 (1/3, Wilson [0.0615, 0.7923]) |
| question-level completeness | 0.3333 (1/3, Wilson [0.0615, 0.7923]) |
| claims needing review | 0 |
| critical wrong values | none observed |
| critical claims contested or awaiting review | none |
| citation precision (judged links) | 1.0 (2/2, Wilson [0.3424, 1.0]) |
| citation precision (lower bound, unjudged = unsupported) | 1.0 (2/2, Wilson [0.3424, 1.0]) |
| unjudged links | 0 |
| citation coverage of material claims | 1.0 (2/2, Wilson [0.3424, 1.0]) |
| unsupported claim rate (reviewed) | 0.0 (0/2, Wilson [0.0, 0.6576]) |
| link validity | 1.0 (2/2, Wilson [0.3424, 1.0]) |
| negative/ambiguous handling | 1.0 (1/1, Wilson [0.2065, 1.0]) |
| false answers on negatives | none |
| unnecessary refusals | 0.3333 (1/3, Wilson [0.0615, 0.7923]) |
| metadata stratum | n/a (0 eligible) |
| technical outcomes | none |
| scope leaks | 0 |
| settled cost (ledger, every attempt) | $0.000187; earlier attempts $0.000000, pending $0.000000, unknown $0.000000, reconciled attempts 0, retried rows 0 |
| latency p50/p95 ms | 16.4/22.02 (n=4, sequential, single process, one call at a time) |

## Finalist comparison

```json
{
 "baseline": "K1-f5b26e4852",
 "candidate": "K0-9abbc718ff",
 "claim_correctness_gain": -0.3334,
 "negative_handling_gain": 1.0,
 "new_critical_wrong": [],
 "p95_ms": [
  24.46,
  22.02
 ],
 "settled_micro_usd": [
  386,
  187
 ],
 "note": "development evidence for the owner's selection; small denominators: read the intervals"
}
```
