`_Session` is ASGI middleware applied to `/api/*` only, in this order:
1. A non-GET request whose `Origin` host is neither the request `Host` nor (with `BIDMATE_LOCAL_MEMBER` only) a dev origin on :8510 gets 403.
2. A `Login.problem` (missing sign-in settings) gets 503 on every call.
3. The sign-in routes pass through.
4. Without a valid `bidmate_session` cookie the call gets 401.
5. `X-BidMate-Account` naming a different member gets 409 with `X-BidMate-Account-Changed`.
6. A state-changing call without that header gets 428.

Then `service.bind_request` sets the context variables `KEY_SESSION`, `REQUEST_MODEL` and `BILLING_SCOPE` to the member's current key session, model and billing project for the request. Everything it starts (executor submits via `contextvars.copy_context`) keeps those values, so a later key change never affects running work.