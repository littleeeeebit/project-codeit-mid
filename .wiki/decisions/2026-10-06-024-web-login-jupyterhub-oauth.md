---
scope: project
severity: preference
triggers: []
domain: ''
title: "Members sign in with their JupyterHub account; BidMate serves publicly on codeit:8501"
pr: null
merged: null
branch: "web-login-jupyterhub-oauth"
---

# Members sign in with their JupyterHub account; BidMate serves publicly on codeit:8501

What. This reverses the no-login decision (owner, 2026-09-30, reaffirmed 2026-10-01 and kept by the SSH-tunnel deployment of 2026-10-06). BidMate is now an OAuth client of the team's JupyterHub on `codeit` (the hub service `bidmate`). The six members open <http://35.255.64.243:8501> and sign in with the same account they use on :8000. Every `/api` route except sign-in needs the session. Only the hub usernames on `BIDMATE_ALLOWED_USERS` get one, and other hub users get 403. The session's username replaces the typed 이름 field and the `X-Member` header as the name recorded on paid requests, reviews, corrections and audit rows. Each member's OpenAI key from 설정 belongs to their account rather than to one browser, still only in memory. Without the hub settings the API refuses every call; `BIDMATE_LOCAL_MEMBER` is the explicit switch for a local run with no hub.

Why. The owner wanted members to reach BidMate in a browser with no SSH tunnel, using exactly the credentials they already have on :8000. Without a login, publishing the port would have opened the shared OpenAI budget and every admin page to the internet; that was the only reason for the loopback-plus-tunnel deployment. The hub's OAuth provider is its supported way to lend identity to a service: BidMate never sees or stores a password, and the hub stays the one place passwords change. The hub accepts more accounts than the team (it runs servers for other `spai*` users), so BidMate keeps its own allowlist of six. Binding the key to the account makes it follow the person across browsers, and attribution matches who pays. Fail-closed matters on a public port: a missing setting must never quietly bring back open access.

Rejected. Posting credentials to the hub's login form over loopback: no hub change, but BidMate would handle raw passwords and depend on the form's HTML. A BidMate password table: it drifts from the hub as passwords change. PAM: the service user would need shadow access. Trusting every hub user. Owner-only settings or maintenance pages: every allowed member keeps all pages. HTTPS through Caddy on 443, or a self-signed certificate on 8501: the first needs changes to the course project's shared firewall rules, the second shows browser warnings. Plain HTTP is used, like the hub itself.

Consequences. The hub accepts exactly one redirect URI per client, so a browser on the SSH tunnel moves to the public address to sign in. The tunnel still reaches the port but is no separate way into the screens. The address is ephemeral: when it changes, the hub's `oauth_redirect_uri`, `BIDMATE_HUB_URL` and `BIDMATE_OAUTH_REDIRECT_URI` change together, and the hub restart stops the team's notebook servers (runbook 3.1). Sessions live in memory, so a BidMate restart signs everyone out, as it already dropped every key.

Source. Branch `web-login-jupyterhub-oauth`
