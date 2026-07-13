#!/usr/bin/env sh
set -eu
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
exec python -m uvicorn business_app.main:app --host 0.0.0.0 --port 8080
