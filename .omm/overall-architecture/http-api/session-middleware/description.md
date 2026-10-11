`_Session` is a pure ASGI middleware applied to /api/* only, with checks in this order:
1. A non-safe method from a foreign Origin gets 403. :8510 dev origins are trusted only when BIDMATE_LOCAL_MEMBER is set.
2. `login.problem` (misconfiguration) gets 503.
3. The /api/auth/login, /callback and /logout paths pass through.
4. A missing session cookie gets 401.
5. An `X-BidMate-Account` header that differs from the session gets 409 with `X-BidMate-Account-Changed`.
6. A missing header on a non-safe method gets 428.
Once the checks pass, `service.bind_request(res, member)` sets the contextvars KEY_SESSION, REQUEST_MODEL and BILLING_SCOPE for the request and every thread it starts (contextvars.copy_context), and resets them afterwards.