# Answer run A-4d3b0b7c65b9 (development finalists)

- dataset `dev` (`65b6e71cf9fa`), population `00838388c28e`, status **complete**
- model gpt-6-luna (reasoning low), prompt grounded-answer-3, output cap 2000; code `b9f2ef750afb` dirty=True
- blind reviews applied: 0

## Finalist `K1-7c8e7f5156` (kiwi_bm25): 4/4 rows

| Metric | Value |
| --- | --- |
| required-claim correctness | 0.6667 (2/3, Wilson [0.2077, 0.9385]) |
| question-level completeness | 0.6667 (2/3, Wilson [0.2077, 0.9385]) |
| claims needing review | 0 |
| critical wrong values | none observed |
| citation precision (judged links) | 1.0 (3/3, Wilson [0.4385, 1.0]) |
| citation precision (lower bound, unjudged = unsupported) | 0.75 (3/4, Wilson [0.3006, 0.9544]) |
| unjudged links | 1 |
| citation coverage of material claims | 0.75 (3/4, Wilson [0.3006, 0.9544]) |
| unsupported claim rate (reviewed) | 0.0 (0/4, Wilson [0.0, 0.4899]) |
| link validity | 1.0 (4/4, Wilson [0.5101, 1.0]) |
| negative/ambiguous handling | 0.0 (0/1, Wilson [0.0, 0.7935]) |
| false answers on negatives | ['dev-absent'] |
| unnecessary refusals | 0.0 (0/3, Wilson [0.0, 0.5615]) |
| metadata stratum | n/a (0 eligible) |
| technical outcomes | none |
| scope leaks | 0 |
| settled cost | $0.000384 (retried rows 0) |
| latency p50/p95 ms | 22.0/25.97 (n=4, sequential, single process, one call at a time) |

## Finalist `K0-abc1cbfed6` (whitespace_bm25): 4/4 rows

| Metric | Value |
| --- | --- |
| required-claim correctness | 0.3333 (1/3, Wilson [0.0615, 0.7923]) |
| question-level completeness | 0.3333 (1/3, Wilson [0.0615, 0.7923]) |
| claims needing review | 0 |
| critical wrong values | none observed |
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
| settled cost | $0.000187 (retried rows 0) |
| latency p50/p95 ms | 17.35/23.64 (n=4, sequential, single process, one call at a time) |

## Finalist comparison

```json
{
 "baseline": "K1-7c8e7f5156",
 "candidate": "K0-abc1cbfed6",
 "claim_correctness_gain": -0.3334,
 "negative_handling_gain": 1.0,
 "new_critical_wrong": [],
 "p95_ms": [
  25.97,
  23.64
 ],
 "settled_micro_usd": [
  384,
  187
 ],
 "note": "development evidence for the owner's selection; small denominators: read the intervals"
}
```
