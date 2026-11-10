#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
# All caches and downloaded runtimes stay inside the project.
export PIP_CACHE_DIR="$ROOT/.cache/pip"
export UV_CACHE_DIR="$ROOT/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$ROOT/.python"
export TMPDIR="$ROOT/.tmp"
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR" "$UV_CACHE_DIR"
PY=""
for candidate in python3.11 python3.12 python3.10; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$(command -v "$candidate")"; break; fi
done
if [ -z "$PY" ]; then
  echo 'Installing a project-local Python 3.11 via uv (no system changes).'
  if [ ! -x .kronos_bootstrap/bin/python ]; then python3 -m venv .kronos_bootstrap; fi
  .kronos_bootstrap/bin/python -m pip install --retries 1 --timeout 20 uv
  .kronos_bootstrap/bin/uv python install 3.11
  PY="$(.kronos_bootstrap/bin/uv python find --managed-python 3.11)"
fi
"$PY" --version
if [ ! -d kronos_env ]; then "$PY" -m venv kronos_env; fi
kronos_env/bin/python -c 'import sys; assert (3,10) <= sys.version_info[:2] <= (3,12), sys.version'
if [ ! -d Kronos ]; then git clone https://github.com/shiyu-coder/Kronos.git; fi
git -C Kronos checkout --detach "$(cat kronos-commit.txt)"
test -f Kronos/requirements.txt
kronos_env/bin/python -m pip install --upgrade pip
kronos_env/bin/python -m pip install -r requirements-kronos.lock.txt -c constraints-kronos.txt
kronos_env/bin/python -m pip check
echo 'Installation complete. No market request or model inference was started.'
echo 'Launch the UI: kronos_env/bin/python kronos_window.py --open-browser'
