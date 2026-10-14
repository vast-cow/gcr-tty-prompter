#!/bin/sh
set -eu

python -c 'import sys; sys.exit(0 if sys.prefix != sys.base_prefix else "Activate your virtualenv before running uninstall-user.sh.")'
python -m gcr_tty_prompter.mode disable
python -m pip uninstall -y gcr-tty-prompter

echo "Uninstalled from the current virtualenv. The virtualenv itself was kept."
