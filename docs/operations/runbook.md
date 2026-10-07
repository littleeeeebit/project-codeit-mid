# Operations runbook (phases 3–4)

This runbook covers launching, giving access, stopping, recovering and reconciling the single shared application, and (sections 9–12) the phase-4 setup, evaluation, sealed run, release, index rollback and backup procedures. Every command below exists in this repository. Steps that depend on a host or network decision the owner has not yet made are marked **owner decision**. Do not read them as configured.

Run the commands with the project environment's interpreter from any directory. Paths in environment variables must be absolute.

## PostgreSQL only and Settings limit

Since the 2026-10-04 cutover the application, billing ledger and retrieval sets live only in PostgreSQL 18.6 + pgvector 0.8.6 (the `bidmate_app` database). There is no other backend and no fallback. Start infrastructure with `./tools/infra/start-postgresql.ps1`, then set `RFP_DATABASE_DSN` to the application database. A missing DSN, an unreachable database, a database without a validated import, or an embedding model outside the compared registry (`models.EMBEDDINGS`, at the dimensions it produces) stops startup with a clear message. Serving follows the activated run's embedding: an OpenAI or Gemini model through its API, a local model on the GPU inside the server process. `activate-run` refuses a run whose model is unregistered or whose local revision or prefixes no longer match the registry, which would otherwise serve keyword-only. After a change to corpus routing (`ROUTE_RULE`), the activated run is no longer served. Requests fall back to the keyword default, and every retrieval lists `activated_run_stale:corpus_route` until `evaluate-retrieval` and `activate-run` record a run under the new rule. The cutover evidence (final import, vector parity, HNSW, fusion gate, needle set, paid end-to-end check) is in the [handover](../../docs/history/postgresql-migration/README.md#postgresql-only-operation-2026-10-04).

Application startup requires the persisted import validation and rechecks artifact hashes. A failed or interrupted validation disables paid admission and blocks startup until validation passes.

The cold archives under `.runtime/archive/` (verified PostgreSQL custom-format dumps) are history only; no code reads them. The previous database's files were deleted on 2026-10-05. The Phase 4 pilot runtime lives in the separate PostgreSQL database `bidmate_pilot_archive` with paid admission disabled; never point `RFP_DATABASE_DSN` at it. Rollback is a `restore-check` of the newest verified dump (`postgresql-2026-10-04-r5`, ledger revision 1712) into an empty database (section 12), which leaves paid admission disabled. Before `paid on`, reconcile every record the replaced ledger wrote after the dump's watermark; after new paid writes, take and restore-check a new dump.

The owner approved paid migration work and a $10 shared operating cap on 2026-10-03. The Settings page edits that shared cumulative cap, records the signed-in member and the reason, preserves settled/unknown amounts, and scales current purpose envelopes. It does not change API keys, provider account quotas, paid admission or historical prices. Every signed-in member has the budget-admin capability; the limit is shared, not a per-person account. Decreasing below committed spend or purpose reservations is refused. CLI equivalents are `set-limit --usd <amount> --actor <name> --reason <reason>` and `set-envelopes --file <absolute JSON> --actor <name> --reason <reason>`.

## 1. One owner, one data directory

- One process owns the database at a time. The serving app and paid CLI jobs take the same database-wide session advisory lock across hosts. A second owner is refused with `GatewayLockError`. Stop the UI before paid maintenance (`build-dense`, `evaluate-retrieval --allow-paid-queries`).
- The ledger, requests, audit events and corrections all live in the PostgreSQL application database. Immutable extraction and index artifacts stay under `RFP_DATA_DIR` on a local disk; a network share is outside the contract.
- The operational config (`RFP_CONFIG_FILE`) holds no secrets. On the shared host no file holds an API key: each member enters theirs on the 설정 page (3.3). On a personal machine, `OPENAI_API_KEY` may stay in the process environment or `.env`.

## 2. Access: JupyterHub sign-in

Members sign in with their JupyterHub account (owner decision 2026-10-06, which replaced the no-login decision of 2026-09-30; `.wiki/decisions`). Every allowed member gets every page: 질문하기, 검증 and 데이터셋 만들기. The hub username is recorded on requests, attempts, reviews, corrections and audit events. Budget administration is owner CLI only (section 5); its `--actor` is recorded on audit events.

- The flow. The page's 로그인 goes to `/api/auth/login`, which sends the browser to the hub's `/hub/api/oauth2/authorize` with a random state, also kept in a 10-minute cookie. The member signs in on the hub, if not already signed in there, and the hub returns to `/api/auth/callback`. BidMate checks the state, exchanges the code at the hub's `/hub/api/oauth2/token` from the server (`BIDMATE_HUB_API_URL`, loopback on `codeit`), and reads `/hub/api/user`. A username on `BIDMATE_ALLOWED_USERS` gets a session cookie (`bidmate_session`, HttpOnly, SameSite=Lax, 12 hours, `Path=/api/`). Browsers scope cookies by host, not port, so a cookie for `/` would also reach the hub's notebook servers on :8000, which run code from hub users off the allowlist. Under `/api/` it never does: :8000 serves `/hub/` and `/user/<name>/` only. Each screen also names the account it loaded as on every call (`X-BidMate-Account`). Tabs share one cookie, so after another tab signs in as someone else, the next call answers 409 and the page reloads as that account rather than acting for it. The session lives only in the server's memory, so a restart signs everyone out. The next 로그인 usually needs no password, because the hub remembers its own sign-in.
- Refusals. A hub user who is not on the allowlist gets 403 and no session. A wrong or missing state gets 400. An unreachable hub gets 502. Every `/api` route except `/api/auth/login`, `/callback` and `/logout` answers 401 without a session, and the screens then send the browser back to sign-in. A state-changing call whose `Origin` is another address (a notebook page on :8000 included) gets 403, and one that names no account gets 428.
- Fail-closed. If any of `BIDMATE_HUB_URL`, `BIDMATE_OAUTH_CLIENT_ID`, `BIDMATE_OAUTH_CLIENT_SECRET`, `BIDMATE_OAUTH_REDIRECT_URI` or `BIDMATE_ALLOWED_USERS` is missing, every `/api` call answers 503 with the names of the missing settings. `BIDMATE_LOCAL_MEMBER` (one developer, no sign-in) is for a personal machine only. It must never be in `server.env` or the unit, and setting it next to the hub settings also answers 503.
- Sealed test rows are never served to the 검증 or 데이터셋 만들기 pages. Only the phase-4 freeze procedure, run by the owner from the CLI, reads `sealed/`.
- Running requests still settle on the server after a sign-out, and the next sign-in shows them under "내 최근 요청" for that account.

## 3. Launch

### 3.1 The team host (live since 2026-10-06)

The shared application runs on the team's existing GCP VM `codeit`. It is the only live ledger and the only gateway owner. Members reach it at <http://35.255.64.243:8501> and sign in with their JupyterHub account (section 2). API keys reach it only through the 설정 page, one per member account, and live only in the service's memory (3.3). The owner's Windows server is no longer live (3.4).

| | |
| --- | --- |
| VM | `codeit` (existing team VM, also JupyterHub), project `sprint-ai-01`, zone `us-central1-c`, Ubuntu 24.04 |
| Machine | `g2-standard-4` (4 vCPU, 16 GB, one NVIDIA L4), 50 GB boot disk shared with the team |
| Address | `35.255.64.243`, ephemeral: it changes if `codeit` is stopped and started. Then follow "When the address changes" below. |
| Listening (BidMate) | uvicorn on `0.0.0.0:8501` (one worker), served at <http://35.255.64.243:8501> through the already-open 8501 rule; PostgreSQL 18.6 on `127.0.0.1:55432` only. JupyterHub is the team's, on `:8000` (proxy) and `127.0.0.1:8081` (hub). |
| Paths | checkout `/srv/bidmate/app`, originals `/srv/bidmate/app/원본 데이터`, runtime `/srv/bidmate/app/.runtime`, venv `/srv/bidmate/venv`, backups `/srv/bidmate/backups`, owned by the `bidmate` service user (mode 750) |
| Secrets | `/etc/bidmate/server.env` (root, mode 600): `RFP_DATABASE_DSN`, `RFP_CONFIG_FILE`, `RFP_SOURCE_DIR`, `RFP_DATA_DIR`, `RFP_PATH_MAP`, and the sign-in settings below. No tracked file and no `.env` holds them. There is no `OPENAI_API_KEY` on the VM in any file; Langfuse tracing is off there. |

Sign-in settings in `server.env` (section 2). They are the only place BidMate reads them from:

```sh
BIDMATE_HUB_URL=http://35.255.64.243:8000                     # where browsers are sent to sign in
BIDMATE_HUB_API_URL=http://127.0.0.1:8000                     # where the server exchanges the code
BIDMATE_OAUTH_CLIENT_ID=service-bidmate
BIDMATE_OAUTH_CLIENT_SECRET=<the bidmate service's api_token in /root/jupyterhub_config.py>
BIDMATE_OAUTH_REDIRECT_URI=http://35.255.64.243:8501/api/auth/callback
BIDMATE_ALLOWED_USERS=spai1302,spai1303,spai1308,spai1316,spai1319,spai1322
```

The hub side is the `bidmate` entry in `c.JupyterHub.services` of `/root/jupyterhub_config.py`: `oauth_client_id` `service-bidmate`, `api_token` (the client secret above), `oauth_redirect_uri` (the same URI as above) and `oauth_no_confirm`. A `bidmate-users` role in `c.JupyterHub.load_roles` grants the six members `access:services!service=bidmate`; without it the hub refuses them with 403 before BidMate sees them. The hub loads both only at start, and restarting `jupyterhub.service` stops every running notebook server, so pick the time with the team.

When the address changes. Update the hub's `oauth_redirect_uri`, `BIDMATE_HUB_URL` and `BIDMATE_OAUTH_REDIRECT_URI` to the new address together, then restart `jupyterhub.service` (notebooks stop) and `bidmate`. Until then sign-in fails, because the hub returns browsers only to the registered address. Then update the Address line above and the address in README.

Firewall. `codeit` sits on the project's shared `default` network, whose rules (owned by the course project, not by BidMate) open tcp:22 and several other ports, 8000 and 8501 among them, to every instance. BidMate uses the open 8501 rule as it is and changes no rule. It listens on all addresses only because every `/api` route requires a JupyterHub sign-in by an allowlisted member, and without the sign-in settings it refuses every call. Plain HTTP is used, the same as the hub on :8000: the session cookie and the hub's own login cross the network unencrypted.

Machine type. On 2026-10-06, before choosing the host, the owner host's `bidmate_app` had `active_run` = `H-0fffb2a6ec`: hybrid, `text-embedding-3-large` @1536 through the OpenAI API, `reranker: null`. Serving needs no local embedding model or reranker, so a CPU VM would do. The owner chose the existing `codeit` instead; BidMate's venv has no torch and does not use its L4. Activating a local-model row (검증 › 검색 구성 비교) would need torch with CUDA in `/srv/bidmate/venv` and GPU memory left over by the team's notebooks; do neither without the owner.

Reaching it. Open <http://35.255.64.243:8501>, press 로그인 (JupyterHub 계정으로 로그인), and sign in with the same username and password as on :8000. The header then shows that username and 로그아웃. Nothing else is needed: no SSH, no key.

The SSH tunnel still reaches the server. Signing in through it moves the browser to the public address first, because the hub returns browsers only to the one address it has registered. The tunnel is therefore no separate way into the screens, only a way to reach the port (an unauthenticated `curl -i http://127.0.0.1:8501/api/info` through it answers 401, as from anywhere):

```sh
ssh -i <your private key> -N -L 8501:127.0.0.1:8501 <your username>@35.255.64.243
```

`-i` can be dropped only when the key has one of ssh's default names (`~/.ssh/id_ed25519`, `id_rsa`, …); otherwise the server answers `Permission denied (publickey)`. A key that gcloud created is `~/.ssh/google_compute_engine` (the owner's, in PowerShell: `-i "$HOME/.ssh/google_compute_engine"`). Write key paths with forward slashes: chat apps drop a backslash before `.`, and PowerShell then reads `$HOME.ssh` as an empty property, leaving `\google_compute_engine`. If something on the member's PC already uses 8501, forward another local port (`-L 8502:127.0.0.1:8501`) and open that one.

Who can use the screens: hub users on `BIDMATE_ALLOWED_USERS`, the same six as the hub's own `allowed_users`. Other accounts on the hub (it also runs servers for other `spai*` users) get 403. On 2026-10-06 a member other than the owner opened all three pages through their own tunnel, before sign-in existed.

Who can reach the VM over SSH (and so read `server.env` and the CLI): the project-wide SSH-key metadata of `sprint-ai-01` (`codeit` does not block project keys; OS Login is off). Each member's existing key is installed as their own user (checked 2026-10-06):

| User | Key fingerprint |
| --- | --- |
| `spai1302` | `SHA256:BW+8ViEOnm64nzB9Eea6U/9stkxJwLUdD5uZN34eIFE` |
| `spai1303` (owner) | `SHA256:zuDvYLEQMSRtzl7xSSfEKxOSFwd4Uiowf0/Dhuaq1Os`, `SHA256:gYM2DId0G7uoteLRqADM7vz+EfznbAKdAoG/8k+YLjI` |
| `spai1308` | `SHA256:gVmamf8+IBf/k5H1vwSA/i3TIc1htH9lS6kWxVBl9oM` |
| `spai1316` | `SHA256:MS2LnwELSurZolhz+kSFv1aauY12WVxOpWzd9/njeLQ` |
| `spai1319` | `SHA256:tEQoIDgZcPgc2G4UzRRCjzPQ6kiM1EdRNY7DWiU35Us` |
| `spai1322` | `SHA256:+3WPv1N3cz+axixGWGun5UrafgqNSKZcLBtkwhDb7Rk` |

All six are in `google-sudoers` on `codeit`, so each can also read `/etc/bidmate/server.env`. The secrets are kept out of the repository, not from the team. A key added to project metadata (Console › Compute Engine › Metadata › SSH keys) gives that person the same reach. Removing it there revokes it.

### 3.2 Linux start path on `codeit`

`tools/infra/start-postgresql.sh` replaces `tools/infra/start-postgresql.ps1`: it creates `.runtime/postgresql.env` (mode 600) once and starts `compose.postgresql.yaml` on loopback port 55432. `tools/infra/bidmate.service` (installed as `/etc/systemd/system/bidmate.service`) runs it before uvicorn on `0.0.0.0:8501 --workers 1` as the `bidmate` user, with `/etc/bidmate/server.env` (which must hold the sign-in settings of 3.1, or every API call is refused). It starts at boot and stops with SIGINT (the controlled stop of section 4).

```sh
sudo systemctl status bidmate          # or: restart / stop; logs with journalctl -u bidmate
sudo ss -ltnp | grep -E ':(8501|55432) '   # 0.0.0.0:8501 python (uvicorn), 127.0.0.1:55432 docker-proxy
curl -si http://127.0.0.1:8501/api/info | head -1   # HTTP/1.1 401: sign-in is enforced (503: settings missing)
sudo /srv/bidmate/app/tools/infra/bidmate-cli.sh budget-status   # any owner CLI command, with the service's environment
```

Deploying a code change: the repository is private, so ship a bundle from the owner host (`git bundle create bidmate.bundle <branch>`, `scp` it to the VM), then on the VM `sudo -u bidmate git -C /srv/bidmate/app pull /tmp/bidmate.bundle <branch>`. Rebuild `web/out` there (`cd web && npm ci && npm run build`) when screens changed, then `sudo systemctl restart bidmate`. When `tools/infra/bidmate.service` changed, copy it over `/etc/systemd/system/bidmate.service` and run `sudo systemctl daemon-reload` before the restart; the installed unit is a copy, and a stale `ExecStartPre` path stops the service from starting. Stop the service before any paid CLI job or `backup` (section 1).

The VM serves the owner host's restored database, whose rows keep Windows paths (`C:\Users\dasdk\PycharmProjects\project-codeit-mid\...`). `RFP_PATH_MAP=C:/Users/dasdk/PycharmProjects/project-codeit-mid=/srv/bidmate/app` maps them onto the copied originals and runtime. Every reader of a recorded original, extraction or index path goes through `postgres.host_path`. Paths written on the VM are Linux paths and need no mapping.

### 3.3 The move (2026-10-06)

1. The owner's server on `127.0.0.1:8501` was stopped with no queued or running request and no reserved or dispatching attempt.
2. `backup --destination <repo>\.runtime-backups\vm-cutover-2026-10-06 --actor owner`: 33 tables, ledger revision 8902, spent $2.481366, pending $0, unknown $0, 2,965 settled attempts (reserved $4.001802, settled $2.032548), 225 requests.
3. The originals (`원본 데이터`, 164 MB), the runtime (3.9 GB, without `archive/`, logs, `*.env` and private files) and the backup were copied to `codeit` with `tar | ssh`. The VM keeps its own `.runtime/postgresql.env`.
4. `restore-check` into the VM's empty `bidmate_app` (`RFP_RESTORE_DATABASE_DSN`, `RFP_DATABASE_DSN` unset, `RFP_PATH_MAP` set) passed all 255 checks. The receipt shows revision 8902, spent 2,481,366, pending 0, unknown 0, the same attempt totals and 225 requests, before and after restart recovery. Paid admission is off.
5. `systemctl enable --now bidmate`. Startup accepted the validated import and its artifact hashes through the path map.
6. Through `ssh -L` from the owner's PC, 질문하기, 검증 and 데이터셋 만들기 loaded. For the last answered request (`0f380c5a-…`), the cited evidence opened, and its original HWP (4.2 MB) downloaded with a SHA-256 equal to its `source_hash`.

7. Reconciliation: neither ledger recorded an attempt after the dump (the owner's copy was stopped and set to `paid off`; the VM's only new request was refused by `postgresql_maintenance`). With the service stopped, the owner ran `set-limit --usd 20` (the owner's limit, $20) and `paid on`. A real 질문하기 answer through the tunnel then completed with 2 claims and settled $0.000843.

The ledger's spent amount ($2.482209 after that answer) is the app's settled attempts ($2.03) plus `external:pr8-pilot-ledger` ($0.448818, imported on 2026-10-02). The OpenAI dashboard showed $2.01 on 2026-10-06. The ledger stays the higher, conservative figure; lowering it is an owner `adjust` with evidence (section 5).

No file on `codeit` holds an API key: not `server.env`, not a `.env`, not the database. Each member who wants paid answers enters their own key on the 설정 page ("내 OpenAI API 키") after signing in. The server first checks it against the serving model with a free model-metadata read. It then keeps it in process memory under a random key session that belongs to the signed-in account; no cookie carries it. The same account sees it configured in any browser it signs in from, and no other account sees or uses it. Every paid stage a request starts, including the background jobs it launches (evaluation, judge runs, dataset drafting), pays with the key of the member who sent it. A member with no key, such as a teammate who has not entered one, is refused before dispatch with "OpenAI API 키가 설정되지 않았습니다"; nothing is reserved or charged. With the key, each member picks their answer model: `gpt-5-mini` (the configured default) or `gpt-5-nano`. The team's shared key reaches both. Since 2026-10-07 `gpt-6-luna` is not an answer model, and a config naming it as `generation_model` is refused at load. The key is checked against the chosen model, and the model can be changed later without re-entering the key. Answer generation (질문하기 and 검증 answers) uses the member's model. Answer evaluation runs answer with the configured default (`gpt-5-mini`). Dataset drafting, the judges and AI review keep `gpt-6-luna` on the owner's personal key, whatever the answer model is. A key without `gpt-6-luna`, such as the team key, is refused there before dispatch. The first selection of a model the ledger has no rate for adds its standard rate from `settings.DEFAULT_RATES` (OpenAI pricing page, checked 2026-10-06: gpt-5-mini $0.25 input, $0.025 cached, $2.00 output; gpt-5-nano $0.05, $0.005, $0.40 per million tokens), audited as `register_generation_rate`. Existing rates and the rate version stay. A key without embedding access still answers: the query embedding is refused and retrieval falls back to keywords, recorded as the fallback `hybrid->kiwi_bm25:query_vector_unavailable`. The page shows only whether the account has a key, who entered it and when. The audit log records `set_api_key` with the member and model, never the value. All spending, whoever's key paid, is recorded in the one shared ledger and counts against the shared $20 limit, per member. Signing out or closing the browser keeps the key with the account. Restarting `bidmate.service` (or `codeit`) drops every key and every session, and then the key must be entered again. Free pages keep working, and paid admission stays on. A key change always issues a new key session, and each HTTP request fixes its member's key, answer model and billing project when it begins. Work a request started (its answer, or a background job) keeps those to the end, even if the member picks another model or key meanwhile.

Paid CLI work on `codeit` (`run-answers`, `latency-run`, `run-judges`, `maintain`) has no key there, because `server.env` holds none; it is refused before dispatch. The same work runs from the pages: 검증 (evaluation, judge runs, maintenance) and 데이터셋 만들기 use the key of the member who starts them. A paid CLI run would need the key in that one command's environment; no file may hold it. The six team members are sudoers on `codeit` and could, as root, read a process's memory. The backstop for that is the provider: use this deployment's own OpenAI project key with a $20 budget, so it can be revoked without touching anything else.

### 3.4 The owner host is no longer live

The Windows server on the owner's `127.0.0.1:8501` was stopped for the dump on 2026-10-06. It must not be started against its `bidmate_app` again: that database's ledger stopped at the dump and would spend a second, independent copy of the allowance. After the VM was verified, `paid off` was recorded on that copy, which also turns off its PostgreSQL paid admission. An accidental start there serves free pages but cannot spend. The copy stays until the owner drops it. Local screen work uses the fixture server or a fake-provider config (README).

### 3.5 Local development

Local development and single-host use:

```powershell
cd web; npm ci; npm run build; cd ..        # once per checkout or screen change: writes web/out
$env:RFP_CONFIG_FILE = (Resolve-Path docs/history/postgresql-migration/config.example.json).Path
$env:RFP_DATABASE_DSN = "postgresql://bidmate:<password>@127.0.0.1:55432/bidmate_app"   # password from .runtime/postgresql.env
$env:BIDMATE_LOCAL_MEMBER = "<your name>"   # no hub here: you are this name, nobody signs in
python -m uvicorn rfp_assistant.api:app --host 127.0.0.1 --port 8501 --workers 1
```

The API serves the built screens from `web/out` and the routes under `/api/`, so members need only this one port.
Never run more than one worker: the process owns the request executor and the database-wide paid-gateway lock.

`BIDMATE_LOCAL_MEMBER` turns sign-in off, so a local run stays on `127.0.0.1`. Without it, and without the hub settings of 3.1, the API refuses every call. Only the team host binds `0.0.0.0`, behind its JupyterHub sign-in. The phase-3 report (`report --phase 3`) prints the host from `<RFP_DATA_DIR>/releases/phase-3/team-host.json`, which the owner writes on the team host: `{"host", "reach", "tunnel", "members", "recorded_at"}`.

Paid generation stays disabled until `configure-budget` records the dates, prior use, allowance and cap (see the README). Fake-provider demonstrations use a config file with `{"provider": "fake"}` and optionally `"fake_delay_seconds": 4`. Such a config never builds a real SDK client.

### 3.6 The sign-in cut-over (2026-10-06)

The owner approved the hub edit and the switch to `0.0.0.0`. With no paid work pending (`budget-status` pending $0):

1. `/root/jupyterhub_config.py` was backed up to `.bak-2026-10-06`. The `bidmate` service and the `bidmate-users` role were appended (3.1), with the client secret generated on the VM into `/etc/jupyterhub/bidmate-oauth-secret` (root, 600), because the config file is world-readable. The config was loaded with traitlets before the restart. `systemctl restart jupyterhub` stopped the running notebook servers. The hub log shows "Role bidmate-users added", "Creating oauth client service-bidmate" and "Adding external service bidmate".
2. The branch went to `/srv/bidmate/app` as a bundle. `npm ci` and `npm run build` ran in `web/` (the VM had no `node_modules`). `server.env` got the six `BIDMATE_*` settings, with the secret copied from that file, never displayed. Backup: `server.env.bak-2026-10-06`.
3. The first start failed with `FileNotFoundError` for `handoff/postgresql-pgvector/config.example.json`. The VM had run the PR #22 branch, and #23 moved that file to `docs/history/postgresql-migration/`. `RFP_CONFIG_FILE` in `server.env` now names the new path; the two files are byte-identical. A deploy that crosses a rename must check `server.env`'s paths too.
4. On `127.0.0.1` first: `/api/info` and `/api/auth/me` answered 401, the screens answered 200, and `/api/auth/login` answered 303 to the hub's authorize URL with `client_id=service-bidmate` and an HttpOnly, SameSite=Lax state cookie. The hub sent an unsigned visitor to its login page, and its token endpoint refused a wrong secret with 401 `invalid_client`.
5. The unit from `tools/infra/` (`--host 0.0.0.0`) was installed and restarted. The previous unit is kept as `bidmate.service.bak-2026-10-06`. From outside the VM, `http://35.255.64.243:8501/api/info` answered 401 ("로그인이 필요합니다.") and the screens answered 200.
6. The owner (`spai1303`) opened the public address in a browser with no tunnel. `/api/auth/me` answered 401, then login went through the hub (`oauth2/authorize` 302, `oauth2/token` 200), the callback answered 303 with the session cookie, and `/api/auth/me` answered 200. With the key re-entered on 설정 for that account, 질문하기 answered and opened its evidence, and 검증 and 데이터셋 만들기 loaded (`/api/verify/*`, `/api/gold/*`, `/api/drafting/*` all 200). A teammate's first live sign-in had not happened yet, so their allowlist entries and the per-account key separation were covered only by tests (`tests.test_api.LoginTest`, `ApiFlowTest`) at that point.
7. Review found that the session cookie, set for `/`, also rode to the notebook servers on :8000. The fix (`Path=/api/`, plus account-bound screens) was pulled from a bundle, `web/` rebuilt, and bidmate restarted with nothing pending (`pending_micro_usd` 0, `open_attempts` 0), which signed everyone out and dropped entered keys. From outside, `/api/info` answered 401, and `/api/auth/logout` expired `bidmate_session` at both `Path=/api/` and the first deployment's `Path=/`.

## 4. Request execution and controlled stop

- Paid answers run on a process-owned executor with 6 workers and at most 12 admitted unfinished requests (`request_workers`, `request_admission`). A full queue is refused before any paid work.
- Re-submitting the same request (a rerun, a double click) returns the same request. The same key with a different question is refused.
- Controlled stop: stop the server with Ctrl+C (SIGINT/SIGTERM). The resource owner then:
  1. stops accepting work;
  2. waits up to `shutdown_wait_seconds` (20 s) for running workers and background jobs (gold drafting, development answer evaluation, judge comparison). No new job starts once stop begins;
  3. marks queued and unfinished requests `interrupted`;
  4. closes the SDK client and releases the lock.
- A worker still inside a provider call keeps its attempt `dispatching`. That attempt becomes `unknown` at the next start.
- A worker that has not yet dispatched never starts a new paid stage once stop begins. That covers query embedding and generation, even after its retrieval finishes. The stop check and the `dispatching` marker share one ledger transaction. Such a request ends `interrupted`, and its reservation, if any, is released.
- The stop starts when uvicorn's signal handler ends the server: the API's lifespan shutdown closes the resource owner, and an exit hook covers an interpreter exit without it. Both run before the executor waits for its workers, so a running request stops at its next paid stage instead of finishing first.
- Verifier generation: the 검증 page's paid button generates from the frozen run's own evidence and 기준일 (`verifier_run_id`). It never retrieves again, so a query-vector cache miss cannot switch it to hybrid and no query embedding is paid. The reservation never exceeds the displayed maximum. The maximum is passed into the atomic admission, so even a rate change between estimate and reservation refuses the request (`above_consented_maximum`) without a call. If the index, serving run, prompt or model changed since the run was frozen, the button is withheld and the service refuses the run. Make a new run instead. Page traces run at the settings' evidence limits; limit experiments go through `evaluate-retrieval`.
- Restart: the next owner recovers conservatively and never replays anything:
  - queued and running requests become `interrupted`;
  - `dispatching` attempts become `unknown`, keeping their reserve as pending cost;
  - `reserved` attempts that never dispatched are released.

## 5. Budget recovery

Budget administration is owner CLI only. These commands, and `settle` and `reconcile` below, are ledger-only: they open no provider client and run beside the serving app without taking its gateway lock:

```powershell
python -m rfp_assistant.cli budget-status
python -m rfp_assistant.cli budget-report
python -m rfp_assistant.cli unresolved
python -m rfp_assistant.cli audit --actor <owner>
python -m rfp_assistant.cli adjust --key <unique key> --amount-usd 0.12 --evidence "<where>" --reason "<why>" --actor <owner>
python -m rfp_assistant.cli paid off --reason "<why>" --actor <owner>
```

`budget-report` is the daily owner check. It prints the totals (cap, spent, pending, unknown, available, adjustments), each purpose envelope with its used and remaining amount, each member's settled and pending amount, and the reconciliation watermark: the ledger revision, the latest reconciled interval end with its ID and scope, and the unresolved amount. Every field comes from one read-only snapshot, so it can run while the app serves: a settlement or reconciliation committed meanwhile is wholly in the report or wholly absent. It reads the ledger only. It needs no API key, opens no provider client or gateway, and writes nothing, not even schema statements.

An `unknown` attempt holds its full reservation until evidence arrives. There is no bulk release, and age alone never releases anything. Resolve one attempt with exactly one of these, each with a reason and an audit event:

1. Settle from usage evidence: `settle --attempt-id <id> --prompt-tokens N --completion-tokens N --cached-tokens N --evidence "<dated provider export>" --reason "<why>" --actor <owner>` with the provider's dated usage for that call. Settlement happens exactly once; a duplicate completion changes nothing.
2. Reconcile a closed provider interval. Build a JSON record and import it:

   ```json
   {"reconciliation_id": "2026-10-01-daily", "interval_start": "2026-10-01T00:00:00+00:00",
    "interval_end": "2026-10-01T23:59:59+00:00", "provider_total_micro_usd": 412345,
    "scope": "<provider project, e.g. proj_abc123>", "evidence": "<dated export file or dashboard capture>",
    "covered_attempt_ids": ["<unknown attempt dispatched inside the interval>"],
    "unscoped_attempts": "include", "reason": "<why>"}
   ```

   ```powershell
   python -m rfp_assistant.cli reconcile --file C:\abs\reconcile-2026-10-01.json
   ```

   `reconcile` takes only `--file`. Its audit event is recorded as `owner-cli` with the record's `reason` (default `provider reconciliation`).

   The adjustment is the provider total minus the local settled cost billed to that project in the same interval. Each attempt records the OpenAI project of the key that paid for it (the `openai-project` header the 설정 key check returns), so one member's project total never cancels spending billed to another member's project. Attempts with no recorded project were paid with the server-environment key; that is every attempt before 2026-10-06 and CLI work. `unscoped_attempts` says whether they belong to this scope (`include`) or not (`exclude`). An interval that holds such attempts is refused without it. Covered `unknown` attempts follow the same rule: each must be billed to the reconciled project, or be unscoped with `unscoped_attempts` set to `include`. Otherwise the import is refused, because one project's evidence cannot resolve another project's bill. Evaluation runs started from 검증 answer with the model their estimate and `config.json` record (the configured default, `gpt-5-mini`; runs before 2026-10-07 recorded `gpt-6-luna`), whatever answer model the starting browser chose; that browser's key and project still pay for them. Covered attempts must be `unknown` and dispatched inside the interval; they move to `reconciled`. Re-importing the same record is harmless. The same ID with a different total is refused, so a changed provider total needs an owner correction. Late usage for a covered attempt settles it and adds one compensating negative adjustment (`late-settlement:<attempt>`).
3. Spend outside the gateway (a notebook, a direct key): record it on the admin page under 외부 사용 조정, with a unique key, the amount, evidence and a reason. A key cannot be applied twice.

If the provider's billing scope or owner export is unavailable, keep the attempts `unknown`. The header then warns about the unknown amount, so nobody mistakes local estimates for provider credit.

Freeze. If a settled cost ever exceeds its reservation, new paid work freezes. Inspect token counting and rates first. Then resume on the admin page (유료 호출 상태, with a reason) or with `configure-budget`.

## 6. Cap and pacing

- Header warnings: the highest of 50 %, 75 % and 90 % of the operational cap (spent plus pending), "ahead of pace" when committed spend exceeds the linear share of the project dates, and unknown billing. The $20 percentage is shown separately from these cap warnings.
- At the cap: paid answers are refused before dispatch. Search, filters, basic information, requirement lists, evidence and original downloads keep working.
- Polling: the budget strip polls every 2 s and the request panel every 1 s. Both are read-only; refreshes and polling never dispatch a call.

## 7. Checks

```powershell
python -m rfp_assistant.cli check --phase 3 --provider fake          # service, budget and request-state gate
python -m rfp_assistant.cli load-check --users 6 --provider fake --save   # six concurrent members, temporary ledger
python -m rfp_assistant.cli load-check --users 6 --provider fake --fail-every 3   # injected post-dispatch timeouts
python -m rfp_assistant.cli report --phase 3                          # .runtime/releases/phase-3/report.md
python -m rfp_assistant.cli check --phase 4 --provider fake          # gold, evaluation, sealed run, backup, report
python -m rfp_assistant.cli check --phase 5 --provider fake          # owner lock, backup/restore, allowance, activation/rollback, reconciliation
python -m rfp_assistant.cli check --phase all --provider fake --save # every test; --save records it for release-report
```

`load-check` builds its own temporary corpus and ledger and never touches `RFP_DATA_DIR`. Only `--save` writes its JSON result next to the phase-3 report. A real paid smoke test is a separate owner-approved action with an explicit estimate. It cannot reproduce the races these fake checks exercise.

## 8. Managed verification

`verification.json` is the manifest for the local verification service (`wiki-agent/local-verification`). It lists the committed contracts, the prose documents, and every major flow. Each flow has a kind (`command` or `browser`), one command, the impacted paths, the environments it uses, and the assertions it must observe. Each flow command is `python -B tools/verify.py <flow-id>`. Its last output is exactly one `local-evidence` block: the head, flow, `environment_id`, `test_scope`, and one observation per assertion (`id`, the exact `expected`, the observed `actual`, `pass`). Browser flows also record the requests sent to the served origin, the browser tool, the build head read from the foot of the served 검증 page (`빌드 <sha>`), and the browser actions. Browser flows build `web/` first (`npm run build`; Node.js and `npm ci` in `web/` are prerequisites) and serve it with the API through uvicorn; `repository-gates` also runs `npm run lint` and `npm run typecheck`.

- Owner settings live in `.wiki/verification.local.json`, saved through the local review settings and bound to the manifest digest. The file is git-ignored, and the implementer never supplies it.
- Corpus and origin: the service copies the settings' `env_file` to the checkout's `.env` without exporting it. The runner reads these keys from that `.env`, and from the process environment only for keys `.env` does not set: `RFP_SOURCE_DIR`, `RFP_DATA_DIR`, `RFP_VERIFY_ORIGIN`, `RFP_VERIFY_BROWSER_EXECUTABLE`, `RFP_VERIFY_QUESTION`, `RFP_VERIFY_HWP_DOC_ID` and `RFP_VERIFY_PDF_DOC_ID`. It never reads `OPENAI_API_KEY`. `RFP_VERIFY_ENV_FILE` points the runner at another file; tests use it to stay on the fixture corpus.
  - Dataset flows read the configured corpus through an isolated copy. The database is backed up into a temporary runtime and the artifact folders are linked read-only. Requests, verifier runs and fake ledger rows never reach the configured runtime.
  - An incomplete or unavailable corpus is a failed observation that names what is missing. Under the service, a dataset flow without any corpus also fails. Only a manual run outside the service falls back to the fixture corpus, and its observation says so.
- Provider: every flow uses the fake provider. `OPENAI_API_KEY` is removed from child processes.
- Browser flows serve the app on `RFP_VERIFY_ORIGIN` (default `http://127.0.0.1:8765`; include it in `allowed_origins`). They drive the app with Playwright (`pip install -e .[verify]`) and sign each browser in through `tests/fake_hub.py`, a stand-in for JupyterHub's OAuth provider whose form takes any allowlisted username. `RFP_VERIFY_BROWSER_EXECUTABLE` selects a browser binary; otherwise Playwright's Chromium and then the installed Chrome are tried.
- On Windows, `controlled-stop` sends a real `CTRL_C_EVENT` to a hidden console child.
- The 15 flows cover access, the four answer modes, the request lifecycle, controlled stop, the shared budget, recovery, verifier runs, the repository gates (including `check --phase 4`) and the phase-4 evaluation and release path (`evaluation-release`: its unit tests plus a 31-step CLI walkthrough on a temporary fixture corpus, `tools/verification/release_walkthrough.py`) as commands. Six browser flows cover the consultant answer and evidence, the history, verifier generation, six concurrent named sessions, cap exhaustion with audited owner actions, and keyboard, focus, contrast and narrow layout. `python -B tools/verify.py --list` shows them.
- The evidence block is printed as the last three lines (one compact JSON line), so it survives the service's 80-line tail.

## 9. Setup and the API key

1. Create the environment and install the pinned dependencies (README, "Environment"). The tested host is Windows with Python 3.12; this repository's cloud checks ran on Linux with Python 3.12.
2. Place `원본 데이터/data_list.csv` and `원본 데이터/files/` under the repository, or point `RFP_SOURCE_DIR` at them (absolute path). `RFP_DATA_DIR` (absolute) moves the runtime; the default is `.runtime/` in the repository.
3. API key placement. On the shared host, each member enters their own on the 설정 page; it is held in memory only, per member account (3.3). On a personal machine, `OPENAI_API_KEY` in the process environment or the git-ignored `.env` also works. Never put it in `RFP_CONFIG_FILE`, a report, an export, a screenshot or a commit. Members never receive the key; they use the shared application.
4. `python -m rfp_assistant.cli init --paid-disabled`, then `manifest`, `ingest`, `build-keyword --include-unreviewed` (README).
5. Paid generation stays off until `configure-budget` records the dates, the prior use with its evidence, the allowance, the cap and the confirmed rates.
6. Run `check --phase all --provider fake --save` on the host before the first paid action.

## 10. Phase 4: evaluation, sealed run and release

Plan: [4-evaluation-and-release.md](../plan/end-to-end/4-evaluation-and-release.md). Paid steps are marked **paid**; each runs only with an explicit estimate ID, uses the same gateway and ledger as the application (envelope `gold_eval`), and needs the UI stopped (`run-answers` and `latency-run` take the gateway lock). Nothing below runs a paid LLM judge; disputed items go to blind human review.

### 10.1 Reviewed gold

```powershell
python -m rfp_assistant.cli gold submit --file C:\abs\dev-batch.jsonl --batch dev-b1 --dataset dev --drafted-by <drafter>
python -m rfp_assistant.cli gold submit --file C:\abs\test-batch.jsonl --batch test-b1 --dataset test --drafted-by <drafter>
python -m rfp_assistant.cli validate-gold --dataset dev
python -m rfp_assistant.cli validate-gold --dataset test        # prints IDs and counts only, never question text
python -m rfp_assistant.cli freeze-dataset --dataset dev --actor <owner> --reason "<why this set>"
python -m rfp_assistant.cli freeze-dataset --dataset test --actor <owner> --reason "<why this set>"
```

- Row shape, evidence groups, typed claims and negatives: `.wiki/gold-drafting.md`, section "Phase 4 gold rows".
- AI reviewers approve gold rows, development and sealed; no person approves rows one by one (operating rule in the [overview](../plan/end-to-end/0-overview.md#operating-rule-pipelines-run-reviewers-approve-a-person-picks)). The reviewer's identity, for example `ai-review:codex-gpt-6.1`, must differ from the drafter's (`api-gpt-6-luna`), from the drafting model's and from whoever started the drafting run. The drafter's own approval is refused; the drafter can only withdraw a row by rejecting it with a note.
- Development rows are drafted on `데이터셋 만들기` and reviewed there or with `gold decide --reviewer <identity> --original-inspected`. The reviewer writes a note, confirms the original was inspected, and marks disputed deadlines, amounts, institutions and mandatory conditions. A second reviewer, differing from the drafter and the first reviewer, records the second review (`gold second-review`, or 검증 › 할 일).
- Sealed rows are reviewed through the owner CLI only, by the same kind of AI reviewer: `gold show --candidate-id <id>`, `gold decide --candidate-id <id> --decision approve --reviewer <identity> --original-inspected`, `gold second-review ...`. They live under `.runtime/sealed/`; the 검증 and 데이터셋 만들기 pages never show them, and `evaluate-retrieval`/`plan-run --action answer-finalists` refuse the `test` split.
- A set below the per-type targets (60 + 60) validates as `pilot`. Report it as a pilot; never call it gold.

### 10.2 Retrieval-first development comparison (free)

```powershell
python -m rfp_assistant.cli evaluate-retrieval --dataset dev --runs K0,K1      # D,H reuse cached query vectors
python -m rfp_assistant.cli compare-runs --run-id <K1 run> --run-id <other run>
python -m rfp_assistant.cli draft-activation --runs <run>,<run> --out C:\abs\decision.json
python -m rfp_assistant.cli activate-run --run-id <run> --decided-by <name> --note "..."   # note optional
```

Each run records the Git revision (or says none was available), whether tracked files differed from it, the package-source and metric-code hashes, and the hardware. Two-document rows are scored per document (recall and complete coverage only); gold-2 nDCG@5 uses `(2^grade−1)/log2(rank+1)` counted in required evidence groups, the unit of its ideal (one complete unit per group), with family-grouped bootstrap intervals (seed 20261001, 1000 resamples).

Returned top-5 passages outside every gold label are counted (`ndcg_pool`) and listed per row (`unlabelled_chunks@5` in the run's `traces.jsonl`). They are shown beside the row's nDCG@5 and no longer block activation: the person reads the count in the table when choosing. `compare-runs` still keeps its advisory recommendation provisional (K1, no finalist) while they are unreviewed. When an AI reviewer finds a required fact in one, it is added as a new alternative (a new gold revision), validated, frozen and the matrix rerun.

### 10.3 Answer finalists (paid)

```powershell
python -m rfp_assistant.cli plan-run --action answer-finalists --dataset dev [--runs <selected>,<finalist>]
python -m rfp_assistant.cli run-answers --estimate-id <id> --actor <owner>            # paid, UI stopped
python -m rfp_assistant.cli export-review --run-id <A-run>                            # blind sheet for reviewers
python -m rfp_assistant.cli import-review --run-id <A-run> --file C:\abs\reviewed.jsonl --reviewer <name>
```

- `plan-run` prices every remaining answer exactly as the answer path will (no provider call) and stores an estimate bound to the finalists, dataset population, prompt, model, output cap, rates and every row's token count. It refuses more than two finalists or finalists with different evidence ceilings. The 검증 › 평가·릴리스 section offers the same plan and a consented run on the application's own gateway.
- `--question-id <id>` (repeatable) plans only those development rows, e.g. a rerun of earlier failures. The finalists are still checked against the whole population, and the subset is part of the run identity.
- Each scored row records a pass: the expected status, no technical outcome, and every gold group the packed evidence reached cited. A group counts as reached or cited only when a chunk's source spans carry its whole approved occurrence, graded like retrieval; sharing an element is not enough. Groups retrieval never reached are reported beside it (`gold groups retrieval did not reach`).
- `run-answers` refuses a changed configuration or price ("plan again") and a maximum that no longer fits the envelope or cap. It stops at the first budget refusal or the first new unknown billing.
- Resume by planning and running again: finished rows are kept and never re-sent; a row whose call has unknown billing is skipped until it is settled or reconciled (§5), then runs once more as a recorded new attempt.
- Deterministic scoring covers typed numbers and dates (with qualifiers such as VAT), negatives and link validity. Text claims, partially supporting citations and unlabelled citations are "needs review" or "unjudged" until the blind sheet is imported. Citation precision is reported twice: over judged links and as a lower bound that counts unjudged links as unsupported.

### 10.3a Embedding comparison by answers (paid)

In hybrid serving, `keyword_first` keeps the first six BM25 results in place, so every embedding has the same nDCG@5, and only answers can separate the embeddings. The comparison answers the development set once per retrieval run, K1 first as the baseline:

```bash
python -m rfp_assistant.cli plan-run --action embedding-comparison --dataset dev --runs <K1>,<H-…>,<H-…>   # free
python -m rfp_assistant.cli run-answers --estimate-id <id> --actor owner --workers 6                     # paid
```

- There is no two-run cap; `answer-finalists` keeps its own. The run is `E-…`, which release and freeze never read.
- The estimate records `reasoning_effort`. GPT-5 mini supports `minimal`; its output-token cap covers reasoning and visible answer tokens together, with no separate hard reasoning-token allocation. A different effort or cap produces a different run. Re-price after changing either, and keep the same effort, cap and question IDs for every compared row. Do not mix earlier answers into that run. See [official GPT-5 guidance](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5).
- `--workers` answers that many retrieval runs at once, each run's rows in order. With gpt-5-mini at about 25 s per answer, 12 runs × 55 questions take about 5 h with one worker. The first budget refusal, new unknown billing or error stops every worker before its next row.
- `finalize` writes `.runtime/compare/tables/answer-embedding.{json,md}`; 실험 비교 shows it as 임베딩별 답변. Each row has answer pass rate, required-claim correctness, claim support, critical failures, rejected answers, and its pass-rate difference against the first run. That difference comes with the exact McNemar p-value over the same questions and its Holm adjustment. The table also carries complete support and query p95 from the embedding table, cost and licence. A conclusion line names the rows that differ at the Holm-adjusted 0.05 level, or says that none does. The person activates a row there; the comparison never activates one.
- On `codeit` it runs as owner CLI work. Stop `bidmate` first, because the CLI must own the gateway. Stopping clears every OpenAI key members entered on 설정. Pass the paying key on stdin into that one process's environment, never into a file. From the owner host, `grep '^OPENAI_API_KEY=' .env | cut -d= -f2- | ssh <you>@codeit 'sudo bash run_paid.sh <estimate-id>'`, where the script runs `IFS= read -r OPENAI_API_KEY; export OPENAI_API_KEY`, sources `/etc/bidmate/server.env` and runs `run-answers` as `bidmate` with `sudo -E`. Start `bidmate` again afterwards.
- From Windows Python, send that stdin as UTF-8 bytes with `text=False`. Text-mode pipes can translate LF to CRLF, and Bash `read` leaves the CR in the key, which the SDK rejects as an invalid header. Remove a trailing CR before export (`OPENAI_API_KEY="${OPENAI_API_KEY%$'\r'}"`) and validate the exported key with a free model-metadata read before pausing the service or starting paid work. Never log a protocol exception's header value.

### 10.4 Release freeze and the sealed run (paid, once)

```powershell
python -m rfp_assistant.cli freeze-release --run-id <activated run> --answer-run <A-run> --decided-by <owner> --rationale "<benefit, critical regressions, latency, cost>"
python -m rfp_assistant.cli plan-run --action sealed --freeze-id <F-id>
python -m rfp_assistant.cli run-answers --estimate-id <id> --actor <owner>
```

- `freeze-release` refuses unless the selected run is the activated one and servable, both splits are validated, frozen and unchanged, and a complete development answer run used that run. It records code, metric-code, prompt, model, rates, settings and dataset/review-log hashes in `.runtime/releases/<F-id>/freeze.json` with an audit event.
- The sealed run executes once. Any later change of code, prompt, model, rates, activation or datasets breaks the freeze. An interrupted sealed run resumes under the same freeze; a completed one stands.
- A later run on the same test set must be planned with `--post-test-regression` and run with `--reason`; it is labeled post-test regression. A new reliability claim needs a freshly drafted and independently sealed test set.

### 10.5 Latency sample (paid, bounded)

```powershell
python -m rfp_assistant.cli plan-run --action latency --waves 5 --users 6
python -m rfp_assistant.cli latency-run --estimate-id <id> --actor <owner>
```

Five waves of six concurrent answers (30 samples) on the activated configuration. The result records n, failures, per-wave timing, the first (cold) wave and the warm waves separately under `.runtime/releases/latency/`. It is a preliminary sample, not an SLA; a run with the fake provider is labeled `fake` and is not model latency.

### 10.6 Release report and mentor walkthrough

```powershell
python -m rfp_assistant.cli check --phase all --provider fake --save
python -m rfp_assistant.cli release-report --latest
```

The report (`.runtime/releases/<F-id or draft-date>/report.md` plus `manifest.json`, `coverage.json`, `evaluation.json`, `budget.json`) reads recorded evidence only and never generates. It marks the release `ready`, `limited` or `blocked`: blocked when a hard check failed (critical wrong value, scope leakage, an unresolvable link, the cap or a frozen ledger, a failed check run); limited when a hard check is unverified, a quality target is missed or unmeasured, or the evidence is pilot-only. Copy a sanitized version to `docs/operations/release-report.md`.

Mentor walkthrough on the frozen candidate (record the outcome as `.runtime/releases/<F-id>/walkthrough-results.json` in the shape of the phase-3 browser results):

1. 질문하기: search a project, select it, ask about a fact beyond the opening pages; show a claim, open its evidence (exact quote, location), download the original.
2. Show the shared cost strip before and after (settled cost of that request).
3. Select the duplicated/conflicting record pair and show the conflict state; show a document with missing metadata (기본 정보) and the quarantined/unsupported source.
4. Select two documents and run a balanced comparison.
5. Change the scope while a request runs: the old answer stays history.
6. 검증: the trace of the same question, a comparison of two frozen runs, and the 평가·릴리스 section (development scores, the sealed set as a count only, the release decision).
7. A budget cap and failure-state example: use a temporary fake-provider runtime (`load-check`, or a config with `{"provider": "fake"}` and its own `RFP_DATA_DIR`). Never drain the shared academy balance to demonstrate the cap.

## 11. Comparison runs, activation and rollback

Pipelines run every variant; a person picks from the tables and activates. Nothing below runs on a schedule, and nothing changes what serves until the person activates a row.

```powershell
python -m rfp_assistant.cli compare --matrix lexical|chunking|embedding|reranker [--only <model or profile>]
python -m rfp_assistant.cli compare --approve <estimate id> --approved-by <name>     # a paid row, then rerun
python -m rfp_assistant.cli compare-cap --usd <amount> --actor <name> --reason "..."   # Gemini's own cap
python -m rfp_assistant.cli compare-resolve --attempt-id <id> --charged yes|no --actor <name> --reason "..."   # a Gemini timeout
python -m rfp_assistant.cli golden-counts      # development, sealed and judge-set rows, counted apart
```

- `compare` runs the declared matrix over the development set (own document and whole corpus) and the needle set, reusing cached indexes, vectors, reranker scores and finished rows, and writes `.runtime/compare/tables/<matrix>.json` and `.md`. Run it with the UI stopped when a local model builds a corpus vector set: it holds the GPU for minutes, while the server's own reranker or embedding needs it too.
- A paid corpus build (an uncached OpenAI model, or Gemini) stops as a `needs_approval` row with its priced estimate. So do question vectors not cached yet, such as a development question added after an earlier approval. An approval covers only the payloads its estimate priced, so a new question needs a new estimate. Approve it only after reading the price; OpenAI spending goes through the shared ledger, Gemini through its own cap and ledger. A Gemini call that times out after dispatch, or whose process ended mid-dispatch (startup recovery marks it), leaves an attempt with unknown billing. Gemini stays blocked until `compare-resolve` records whether it was charged (if unsure, say yes). A model that fails to load, runs out of memory or crashes stays a row with its reason; fix the cause and rerun the matrix, which retries every failed row.
- The reranker matrix reranks the pool of whatever hybrid serves now: its keyword index (chunk profile), embedding model, fusion, depth, units and recorded evidence limits. After activating a different row, rerun it. A custom matrix may hold a paid embedding fixed; once its estimate is approved, `compare` opens the paid gateway for it just as for a varied one.
- Serving retires a GPU model as soon as the activated row no longer uses it, at the next question after the switch, including a switch to an API or keyword row.
- 검증 → 검색 구성 비교 shows the tables. Open a row, read its missed questions, then activate it with your name and an optional note; the CLI equivalent is `activate-run --run-id <id> --decided-by <name> [--note ...]` (a decision file still works). Activation switches serving in one transaction and appends the previous configuration to `activations`. Every earlier index directory and vector set stays, so issued citations keep resolving.
- A local embedding model or reranker loads on the server's GPU with the first question that needs it (the first answer after the switch waits for the load); one copy serves every user under a lock. Switching to another local model retires the previous one first; requests still using it finish with it before its memory returns. It loads from the local Hugging Face cache at its pinned revision, so a gated model such as `google/embeddinggemma-300m` serves without `HF_TOKEN` once the comparison has downloaded it. Watch the first answers' latency after the switch.
- Rollback: activate the last known-good row again (the `text-embedding-3-large` hybrid run stays activatable), with a note naming what failed. Charged usage and traces are not rolled back. `report --phase 2` lists earlier activations.
- An activation changes what the sealed freeze recorded; do it before `freeze-release`, never between the freeze and the sealed run.

## 12. Backup and restore

```powershell
python -m rfp_assistant.cli backup --destination D:\rfp-backups\2026-10-02 --actor <owner>
python -m rfp_assistant.cli restore-check --backup D:\rfp-backups\2026-10-02\manifest.json
```

- `backup` takes the gateway lock/write mutex and uses a complete native custom-format dump. It refuses while the serving app owns the gateway, so stop the app first. `restore-check` requires only the backup and `RFP_RESTORE_DATABASE_DSN` for a distinct empty target; the primary database may be lost or its DSN unset. Recovery commits a durable target fence before the atomic native restore and holds gateway/import locks through verification. Any failure keeps startup and paid admission blocked; successful recovery also leaves paid admission disabled. Referenced immutable files currently stay at their managed paths. See the handover for limitations and commands. The backup refuses a relative, non-empty or overlapping destination. Keys and `.env` are not included; the owner backs them up separately.
- `restore-check` verifies schema, settled/pending/unknown/available amounts, attempt states, copied files, extraction artifacts and the active index; unknown reserves stay pending and nothing is replayed. It publishes its authoritative report in `bidmate_recovery.receipt` in the same durable transaction as validation and readiness. Read `SELECT report_json FROM bidmate_recovery.receipt WHERE id=1` on the isolated target; the CLI reports this receipt location. No filesystem success receipt is written. Startup and paid admission reject verified recovery fences lacking a committed successful receipt; earlier restored targets require a fresh isolated restore.
- Real recovery (phase 5): stop the old owner, restore-check the backup, reconcile spending after its watermark, then copy the database into place and start one owner. Never let the old and the restored owner dispatch concurrently, and never reset the allowance.

## 13. Maintenance: one command or the 검증 button

```powershell
python -m rfp_assistant.cli maintain --actor <owner>
```

Or press 유지보수 실행 on 검증 › 유지보수, which runs the same sequence inside the serving process. Nothing schedules it. In order:

1. Backup. Reuses the newest backup whose table digests still equal the database's, ignoring `audit_events`, which the backup itself writes. Otherwise it writes a new one under `RFP_BACKUP_DIR`, or by default under the `.runtime-backups` sibling of the data directory (ignored by Git). From the button, the backup shares the serving process's gateway lock. The command takes the lock itself, so stop the UI first, as for `backup`.
2. Restore check. Restores that backup into a new scratch database `rfp_mrestore_<random hex>` on the same server with paid admission off, checks it, then drops that database. It creates the database itself and refuses a name that already exists, so it never drops a database it did not create; a run killed mid-check leaves its scratch database for the owner to drop by name. A backup is reused, and its passed check trusted, only while everything a restore of it depends on still hashes to what its manifest recorded: the dump, the copied runtime files, and every original, extraction artifact and index file it references, historical ones included. The check is bound to those hashes. If a referenced file is gone, the new backup taken in its place fails too, and the step names the file: restore it from a copy before maintenance can continue.
3. Manifest and ingest. Imports `data_list.csv` and parses only originals whose bytes, parser or inputs changed. A reparse moves a source's active extraction, but serving keeps searching the extraction its activated index was built from until a rebuilt index is activated. Citations and document lists report that served extraction's own review state, from its reviews and fidelity verdicts. A review of the new extraction does not certify the served text. A reparse that fails quarantines the source, but the served extraction stays answerable until an index without it is activated. The quarantine shows on the ingestion screen.
4. HWP fidelity. Checks only HWP extractions that have no verdict at the current fidelity version, which means the ones ingest just created or changed.
5. Keyword index. Builds, or reuses, the served index's profile and review scope over the current extractions. It does not activate it.
6. Embedding. Embeds the rebuilt index's uncached chunks with the activated embedding model. A paid model stops the run as `needs_approval` with its priced estimate. Approve it with `compare --approve <estimate id> --approved-by <name>`, then rerun; every earlier step is reused. A keyword-only serving run skips this step.
7. Regression. Writes the `regression` table (검증 › 검색 구성 비교 › 회귀 (유지보수)): K1 and the serving configuration on the served index and, if it changed, on the rebuilt one. To serve the rebuilt index, activate its row there. Each row is graded on the questions pinned to the extractions its own index holds, so the served rows keep their frozen population after a reparse. The questions whose evidence a reparse replaced cannot be graded on the rebuilt rows. The table, its screen and the step list them under `needs_evidence_review`, and the run ends as `unverified` rather than `complete` until their evidence is reviewed again.
8. Report. Refreshes `golden-counts`, which shows both the development set and the judge set, and writes `.runtime/maintenance/reports/<run id>.md` and `.json`.

A failed step stops the sequence. The report and the screen name the step and its reason, and later steps stay `pending`. `maintain` exits 0 only for `complete`; `unverified`, `failed` and `needs_approval` exit 1. Every run records the serving run, index and vectors before and after, and reports `Serving unchanged`. Maintenance never activates anything. A rerun with no changed input reuses every step and reports `Provider calls during the run: 0` and `Everything reused: True`.
