`_metered_chat(stage, step, messages, response_format, input_tokens, max_output_tokens, ceiling, on_delta)` handles one paid call:
1. `budget.reserve`. `above_consented_maximum` becomes `clarification_required`; any other refusal becomes `budget_blocked`.
2. `paid_refusal()`. With no key it releases the attempt and returns `technical_error`.
3. `budget.mark_dispatching` with `_dispatch_guard`. A cancel or interrupt raises `_Stop`; a ledger refusal becomes `budget_blocked`.
4. `transport.chat` inside a Langfuse generation span.
5. On a `ProviderError`, it calls `release(confirmed_pre_execution=True)` when execution cannot have happened, and `mark_unknown` otherwise.
6. With no usage it calls `mark_unknown`. Otherwise it calls `budget.settle`; a settlement exception marks the attempt unknown.

It returns the provider response, or the `fail(...)` outcome.