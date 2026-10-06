`_metered_chat` is the single paid-call protocol for chat stages:
1. `budget.reserve` atomically reserves the maximum cost against the cap and envelope (purpose `interactive`, or `gold_eval` through `answers.PinnedResources`). It can also refuse `above_consented_maximum` for frozen runs.
2. `res.paid_refusal()` checks for a missing key and, if so, releases the reservation.
3. `budget.mark_dispatching` runs `_dispatch_guard` (the request's stop check) in the same transaction.
4. `transport.chat` makes the call.
5. The attempt is settled from usage, released when the provider confirms a pre-execution 4xx, or marked `unknown` on timeout, connection loss, missing usage or a failed settlement.
It never retries.