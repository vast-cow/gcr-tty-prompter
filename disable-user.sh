#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
VENV="$DATA_HOME/gcr-tty-prompter/venv"
MODE="$VENV/bin/gcr-tty-prompter-mode"
PYTHON="$VENV/bin/python"

if [ -x "$MODE" ]; then
  exec "$MODE" disable
fi

# Upgrade-friendly fallback: an older installation may already have the
# virtualenv and dbus-next, but not the newly added mode console script.
if [ -x "$PYTHON" ]; then
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
  export PYTHONPATH
  exec "$PYTHON" -m gcr_tty_prompter.mode disable
fi

echo "gcr-tty-prompter is not installed. Run ./install-user.sh first." >&2
exit 1
