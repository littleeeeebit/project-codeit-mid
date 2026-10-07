`_Session.__call__` checks every /api/* request in this order:
- A non-GET/HEAD/OPTIONS request whose Origin host is not Host gets 403. The :8510 dev origins are also trusted, but only with BIDMATE_LOCAL_MEMBER.
- If `Login.problem` is set (sign-in not configured), every call gets 503.
- The three sign-in routes pass through.
- A request without a live `bidmate_session` gets 401.
- If `X-BidMate-Account` names another member, the response is 409 with `X-BidMate-Account-Changed`.
- A state-changing call that names no account gets 428.
It then stores the member in `scope.state` and calls `service.bind_request(res, member)`. That sets the `KEY_SESSION`, `REQUEST_MODEL` and `BILLING_SCOPE` contextvars, which every executor task or job started from this context inherits, and resets them afterwards.