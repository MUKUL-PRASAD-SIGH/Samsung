#!/usr/bin/env bash
# One-command setup for Kairos on Linux / macOS:  ./setup.sh [--quick] [--check] [--no-frontend] ...
# Finds a Python 3.10-3.12 interpreter and hands over to scripts/setup.py (all options are passed through).
set -euo pipefail
cd "$(dirname "$0")"

for cand in python3.11 python3.12 python3.10 python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)' 2>/dev/null; then
    exec "$cand" scripts/setup.py "$@"
  fi
done

# No suitable interpreter: if uv is installed it can fetch one on its own.
if command -v uv >/dev/null 2>&1; then
  echo "No Python 3.10-3.12 found; using uv to provide Python 3.11 ..."
  exec uv run --no-project --python 3.11 scripts/setup.py "$@"
fi

cat >&2 <<'EOF'
Kairos needs Python 3.10, 3.11 or 3.12 and none was found (3.13+ is not supported yet by the speech dependencies).
  Debian/Ubuntu:  sudo apt install python3.11 python3.11-venv
  macOS:          brew install python@3.11
  Arch:           yay -S python311            (or use pyenv / uv: uv venv --python 3.11)
  Or skip Python entirely:  docker compose up --build     (see docs/00_Judges_Guide.docx)
EOF
exit 1
