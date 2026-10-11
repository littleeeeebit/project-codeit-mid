`UpdateWatch.from_env` is enabled only when BIDMATE_UPDATE_REQUEST and BIDMATE_UPDATE_STATE_DIR are set (on codeit: /etc/bidmate/server.env). A daemon thread compares the running commit with GitHub main every 300 s. `request_update` raises `accepting` under `res._runner_lock`, refuses if `open_paid_work` finds:
- admitted or queued/running requests,
- live jobs,
- open or unknown attempts.
If none are open, it atomically writes the marker file. Per the module docstring, root systemd units then run the update script, which fast-forwards, restarts and rolls back on failure. This module only reads `result.json`.