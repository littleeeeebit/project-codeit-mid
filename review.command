#!/bin/sh
# Stage review app: builds review/ when it changed (npm ci, npm run build), serves it on a free 127.0.0.1 port
# and opens the browser. Double-click in Finder, or run ./review.command. RFP_REVIEW_PYTHON picks another Python 3.
cd "$(dirname "$0")" || exit 1
exec "${RFP_REVIEW_PYTHON:-python3}" review/server.py "$@"
