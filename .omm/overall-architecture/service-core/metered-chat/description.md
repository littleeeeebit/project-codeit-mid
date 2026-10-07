`_metered_chat` is the single paid chat path for a request:
1. `budget.reserve` with the purpose `res.paid_purpose` and an optional consented ceiling. A refusal ends as budget_blocked or clarification_required.
2. `paid_refusal()` releases the reservation when there is no key.
3. `budget.mark_dispatching` runs `_dispatch_guard`, so the stop check happens in the same transaction.
4. `_call_and_settle` calls `transport.chat`, streamed through `on_delta` into `res.partials`, and settles the usage.
A pre-execution ProviderError releases the reservation. Any other failure, missing usage or a settlement error marks the attempt unknown. It never retries.