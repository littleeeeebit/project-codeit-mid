---
scope: project
severity: preference
triggers: []
domain: ''
title: "Operating BidMate on team VM codeit: SSH tunnel connection, real data migration, Linux startup path"
pr: 22
merged: 2026-10-06
branch: "deploy-gcp-vm-ssh-tunnel"
---

# Operating BidMate on team VM codeit: SSH tunnel connection, real data migration, Linux startup path

What. Moved the BidMate public instance to the team's existing GCP VM `codeit` (sprint-ai-01, us-central1-c). The app binds only to `127.0.0.1:8501` (1 worker), and PostgreSQL binds only to `127.0.0.1:55432`. Team members open `ssh -N -L 8501:127.0.0.1:8501 <사용자>@35.255.64.243` with their existing SSH keys and connect to `http://127.0.0.1:8501`. …

Why. `postgres.host_path` + `RFP_PATH_MAP`: Remap Windows absolute paths (original text, extracts, index manifests) recorded in the DB to Linux paths. Everywhere that reads these paths (restore-check, startup verification, original text download, ingest, OCR, dense/keyword index load) goes through this function. Test: `tests.test_postgres.SQLBoundaryTests` - Linux startup path: `tools/start-postgresql.sh` (replacing .ps1), `tools/bidmate.service` (systemd), `tools/bidmate-cli. …

Source. PR #22 · `deploy-gcp-vm-ssh-tunnel`
