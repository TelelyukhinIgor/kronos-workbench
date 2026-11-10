#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x kronos_env/bin/python ]; then
  echo 'Run bash setup_kronos.sh first, then open Launch.command again.'
  read -r -p 'Press Enter to close.'
  exit 1
fi
exec kronos_env/bin/python kronos_window.py --open-browser
