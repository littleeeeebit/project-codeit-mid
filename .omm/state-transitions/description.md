Five interacting lifecycles govern what may happen next:
- the request row's status,
- the ledger attempt's billing state,
- the maintenance run's status in state.json,
- the update fence,
- startup and shutdown recovery.
A cancel or interrupt never kills a provider call already sent; it only blocks the next dispatch, and the sent call still settles or stays unknown.