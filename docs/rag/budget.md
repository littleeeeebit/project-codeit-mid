# Tracking and protecting a shared 20 dollar API allowance

The primary percentage should be dollars spent divided by the $20 allowance. Input tokens, output tokens, cached tokens, and embeddings have different prices, so “percentage of tokens used” has no single correct denominator. Show token counts underneath the money-based progress bar and show in-flight reservations separately.

Proposed operating assumption: all six members' paid calls, including notebooks and evaluation jobs, pass through one shared gateway. If anyone uses the raw academy key outside it, the app cannot know that spending in real time. Display the scope of tracking and the last provider reconciliation time; do not call a local estimate the actual remaining provider credit.

## Verified prices and a planning example

The following standard text rates were checked in official OpenAI model documentation on 2026-09-30. USD amounts are per one million tokens. Access for the academy key has not been tested; check the model catalog, price, and permissions again before first implementation use.

| Model | Uncached input | Cached input | Output | Source |
| --- | --- | --- | --- | --- |
| `gpt-4o-mini` | $0.15 | $0.075 | $0.60 | [Model pricing](https://developers.openai.com/api/docs/models/gpt-4o-mini) |
| `gpt-4.1-mini` | $0.40 | $0.10 | $1.60 | [Model pricing](https://developers.openai.com/api/docs/models/gpt-4.1-mini) |
| `text-embedding-3-small` | $0.02 | Not assumed | Not applicable | [Embedding pricing](https://developers.openai.com/api/docs/models/text-embedding-3-small) |

For these text-only models, calculate each generation attempt as:

```text
generation_cost = ((input_tokens - cached_input_tokens) * input_rate
                 + cached_input_tokens * cached_rate
                 + output_tokens * output_rate) / 1_000_000
embedding_cost = embedding_input_tokens * embedding_rate / 1_000_000
spent_percent = 100 * cumulative_spend_estimate / 20
committed_percent = 100 * (cumulative_spend_estimate + reservations) / 20
available_to_schedule = operational_cap - cumulative_spend_estimate - reservations
```

OpenAI input counts include cached input, so do not bill cached tokens a second time. Record provider-reported totals, model ID, endpoint, service tier, rate version, and price snapshot. Do not apply this formula unchanged to models with cache-write fees, tools, audio, or other billable categories. Unknown model/rate combinations must fail before the paid call.

Illustrative request: 4,000 uncached input tokens, 800 output tokens, and a 100-token query embedding costs $0.001082 on the proposed baseline. Six members making ten such requests a day for 28 days produce 1,680 requests, or $1.81776. This is arithmetic on assumed request sizes, not a measured forecast. Longer comparisons, experiments, retries, indexing, and external calls add costs.

The full-document audit recovered 7,420,390 text characters from 98 parsable originals. That is not an embedding token count: count the final unique chunk payloads after headings, table headers, overlap, and any normalization. Repeated preprocessing by six members wastes more than a shared persisted index.

## Suggested envelope and pacing

| Purpose | Proposed allocation |
| --- | --- |
| Initial and incremental embeddings | $1 |
| Golden dataset drafting and limited LLM answer evaluation | $3 |
| Six-member interactive use | $12 |
| Untouched safety reserve | $4 |
| Total | $20 |

The normal operational cap is $16, leaving the $4 reserve unused. Record prior use of this allowance before enabling calls; do not initialize the balance to zero spent just because the app is new. If existing use leaves less than this envelope, scale planned jobs and pacing to the actual remainder.

Use the actual start/end dates for a cumulative pacing line. A 28-day example gives interactive use roughly $0.4286 per day for the team, or $0.0714 per member if shared equally. Individual totals are attribution, not six independent $20 allowances. Category allocations are guardrails and can be reallocated explicitly without increasing the global cap.

Warn at 50%, 75%, and 90% of the operational cap, and whenever use runs ahead of the planned timeline. At the cap, retain keyword search, metadata filters, and source browsing; reject new paid stages. The project allowance does not reset at a UTC month boundary, even if a provider monthly control does.

## Reservation and settlement contract

1. Authenticate the member on the server; attach purpose, pipeline request ID, and attempt ID. Never trust a freely supplied client `member_id` for budget authority.
2. Before a paid embedding request, reserve its bounded input cost. After retrieval and final context packing, count the complete serialized generation request, include message overhead conservatively, and reserve uncached input plus the maximum allowed output. Do not assume a cache hit.
3. Check and reserve inside a short SQLite `BEGIN IMMEDIATE` transaction so six simultaneous requests cannot each spend the same remainder. Commit before the network call; never hold the database lock during inference. [SQLite transactions](https://www.sqlite.org/lang_transaction.html) describe this write-lock behavior.
4. Persist the reservation before dispatch. Every paid stage and network attempt gets a ledger record, including index jobs and retries. Bound or disable hidden SDK retries so the ledger can account for actual attempts.
5. When usage arrives, settle once using a unique provider response/attempt identity. Replace the reservation with measured token-based cost; do not add both to the spent total. Return unused reservation atomically and notify the UI.
6. For a confirmed rejection before execution, release the reservation. A disconnect, timeout, cancellation, or missing final usage is an unknown billing outcome: retain a conservative reservation and mark it for reconciliation. A TTL expiry alone is not evidence that a request cost zero.
7. Recover pending attempts after a restart. Keep adjustments and reconciliation records auditable rather than rewriting past token counts.

Suggested ledger fields: request and attempt IDs, member, purpose, model, index/prompt version, UTC start/finish, response ID, raw usage, price version, estimated/settled microdollar cost, reservation, and billing state. Use integer microdollars with conservative rounding or Decimal for financial calculations. Never round each request to cents before summing.

## What real time can truthfully mean

Reservations update immediately when a call starts. Measured token-based cost updates when final usage arrives. During generation show “in progress, up to $X reserved,” rather than pretending every streamed character is a billable token count. The shared progress bar can refresh on every ledger change and poll every one or two seconds for other members' activity.

[Chat Completions streaming](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) documents `stream_options.include_usage`; interruption can prevent its final usage chunk. Proposed first endpoint: Chat Completions, with `prompt_tokens`, `completion_tokens`, and `prompt_tokens_details.cached_tokens` stored from final usage. Responses has a different event/schema contract; do not mix field names between APIs.

The [organization Usage and Costs reference](https://platform.openai.com/docs/api-reference/usage/costs) is a provider reconciliation route. Confirm the academy owner's administrative permissions and project scope; do not assume the supplied inference key can read organization billing. Without that access, an owner can provide dated usage exports or a dashboard snapshot. Recent outside calls remain unknown until reconciliation.

Reconcile only a known closed time interval for the same project/allowance. Record `provider_cost_for_interval - local_cost_for_same_interval` as an adjustment and then add local costs after the watermark. Do not add provider totals to all local totals: that counts the same spending twice. If account scope includes other teams, it is not a valid adjustment for this $20 pool without filtering.

[OpenAI spend controls](https://developers.openai.com/api/docs/guides/spend-limits) now distinguish alerts from enforceable hard limits; enforcement can lag slightly. Verify the academy account's available controls. They are a second line of defense, not a substitute for per-request reservations or this project-wide cumulative allowance.

## Existing solutions and cost-saving order

[LiteLLM budgets](https://docs.litellm.ai/docs/proxy/users) already support team/member budgets and virtual keys. Its documentation requires a database for enforcement and warns that database-free global budgets fail open. Reuse it when the team already operates that infrastructure; deploying an additional database and proxy solely for six users may exceed the first delivery's setup budget. The minimal alternative is one shared application gateway and SQLite ledger, with the concurrency checks above.

[Langfuse usage and cost tracking](https://langfuse.com/docs/observability/features/token-and-cost-tracking) provides generation/embedding traces and inferred or ingested costs. It is an observability option, not proof of atomic budget enforcement. Start with stored request traces and add it when their volume or comparison needs justify another service. Added 2026-10-04 as a local, optional stack (README, "Langfuse tracing"): generations carry the ledger's settled cost for display, and admission, reservation and settlement never read Langfuse.

Save money in this order: zero-generation metadata/search routes; one shared incremental index; exact-key caching keyed by query, scope, as-of date, corpus, model, and prompt version; local keyword retrieval and local reranking; bounded final evidence and output; reuse retrieval results across experiments; then optional provider caching. Validate authorization before serving cached answers. Do not introduce semantic answer caching that can conflate different RFPs or deadlines.

[OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching) is model-dependent and requires eligible shared prefixes. Budget assuming no cache savings. [Batch processing](https://developers.openai.com/api/docs/guides/batch) offers a 50% discount with asynchronous completion within 24 hours; use it for nonurgent frozen embedding/evaluation jobs, not the interactive path or a first demo due in a few hours.

Before release, check concurrent reservations near the cap, duplicate settlement, a stream interruption, a billed retry, restart recovery, external spending adjustments, a changed price table, and budget exhaustion with continued free search. A chart without these checks is not budget protection.
