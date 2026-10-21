#!/bin/sh
# UK Flood Sandbox. OpenSWMM 6.0.0a4 ships installers for Python 3.10 to 3.13 only.
cd "$(dirname "$0")" || exit 1
if [ ! -f .venv/installed-v8.ok ]; then
  PY=""
  for v in 3.13 3.12 3.11 3.10; do
    if command -v "python$v" >/dev/null 2>&1; then PY="python$v"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "No supported Python found. OpenSWMM needs Python 3.10 to 3.13."
    echo "Install Python 3.13 (python.org, Homebrew: brew install python@3.13) and run this again."
    exit 1
  fi
  echo "Setting up with $PY ..."
  [ -x .venv/bin/python ] || "$PY" -m venv .venv || exit 1
  .venv/bin/python -m pip install --upgrade pip || exit 1
  .venv/bin/python -m pip install -r requirements.txt || { echo "Setup failed. See the messages above."; exit 1; }
  touch .venv/installed-v8.ok
fi
exec .venv/bin/python server.py
