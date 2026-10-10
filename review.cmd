@echo off
rem Stage review app: builds review\ when it changed (npm ci, npm run build), serves it on a free 127.0.0.1 port
rem and opens the browser. Set RFP_REVIEW_PYTHON to pick another Python 3 than the one on PATH.
cd /d "%~dp0"
set "PY=python"
if defined RFP_REVIEW_PYTHON set "PY=%RFP_REVIEW_PYTHON%"
"%PY%" review\server.py %*
if errorlevel 1 (
  echo The review app stopped with an error; see the message above.
  pause
  exit /b 1
)
