Every paid provider call goes through the same three steps:
1. Reserve the maximum cost atomically against the cap and the purpose envelope.
2. Mark the attempt dispatching inside a transaction that re-checks the request and the gateway.
3. Call the provider once, with no SDK retries, then settle from the reported usage, release when the call provably never executed, or mark it unknown.

`gateway/generation.py` is the only OpenAI SDK call site. Tracing is observability only; admission and settlement never read it.