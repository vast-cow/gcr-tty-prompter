#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

python -c 'import sys; sys.exit(0 if sys.prefix != sys.base_prefix else "Activate your virtualenv before running install-user.sh.")'
python -m pip install "$ROOT"

cat <<EOF
Installed into the current virtualenv.

Enable the TTY prompter:
   gcr-tty-prompter-mode enable

Start the foreground TTY server:
   gcr-tty-prompter-server

Check or disable the user D-Bus override:
   gcr-tty-prompter-mode status
   gcr-tty-prompter-mode disable
EOF
