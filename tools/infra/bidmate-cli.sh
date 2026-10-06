#!/bin/sh
# Owner CLI on the team VM: `rfp_assistant.cli` with the service's environment (/etc/bidmate/server.env), as the
# service user. Example: /srv/bidmate/app/tools/infra/bidmate-cli.sh report --phase 3
set -eu
exec sudo sh -c 'set -a; . /etc/bidmate/server.env; set +a; cd /srv/bidmate/app
exec sudo -E -H -u bidmate /srv/bidmate/venv/bin/python -m rfp_assistant.cli "$@"' bidmate-cli "$@"
