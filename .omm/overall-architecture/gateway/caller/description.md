service._metered_chat runs two stages: query_rewrite (from _rewrite) and generation (from _paid_answer). It reserves with an optional ceiling, which is the frozen verifier estimate. Then it handles each outcome:
- not admitted: fail('budget_blocked')
- no key (Resources.paid_refusal): release with reason provider_unavailable
- DispatchRefused for cancelled or interrupted: raise _Stop
- a 4xx before execution: release(confirmed_pre_execution=True)
- a timeout, 5xx or any other exception: mark_unknown
- a response with no usage: mark_unknown
- a failed settle: mark_unknown
It never retries.