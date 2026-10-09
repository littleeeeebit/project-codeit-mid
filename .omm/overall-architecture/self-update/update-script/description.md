`update.sh`, run as root, works through these steps:
- Writes `result running`, then removes the marker.
- If `$STATE/deploying` exists, resumes a cut-off run from the recorded commit (RECOVERING=1, which forces a dependency reinstall and a web rebuild).
- Refuses a dirty checkout, then fetches main from the fixed remote as the service user.
- Returns `up_to_date` if nothing changed; otherwise requires a fast-forward and refuses a changed `bidmate.service`.
- Saves `web/out` to `web-out.prev` and records `deploying`, then fast-forwards.
- Runs `pip install -e` only if pyproject or requirements changed, and `npm ci && npm run build` only if web/ changed.
- Checks every absolute path in server.env, runs `systemctl restart bidmate`, then waits up to 300 s for `/api/info` to answer 401 as the health signal.

Any failure after the change triggers `rollback`: `git reset --hard PREV`, restore the saved screens, reinstall dependencies if they changed, restart and health-check again.