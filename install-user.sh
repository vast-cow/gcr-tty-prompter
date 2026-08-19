#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
APP_DIR="$DATA_HOME/gcr-tty-prompter"
VENV="$APP_DIR/venv"
SERVICE_DIR="$DATA_HOME/dbus-1/services"
SERVICE="$SERVICE_DIR/org.gnome.keyring.SystemPrompter.service"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$APP_DIR" "$SERVICE_DIR" "$BIN_DIR"

python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install "$ROOT"

ln -sf "$VENV/bin/gcr-tty-prompter-client" \
  "$BIN_DIR/gcr-tty-prompter-client"
ln -sf "$VENV/bin/gcr-tty-prompter-server" \
  "$BIN_DIR/gcr-tty-prompter-server"
ln -sf "$VENV/bin/gcr-tty-prompter-selftest" \
  "$BIN_DIR/gcr-tty-prompter-selftest"
ln -sf "$VENV/bin/gcr-tty-prompter-mode" \
  "$BIN_DIR/gcr-tty-prompter-mode"

cat > "$SERVICE" <<EOF
[D-BUS Service]
Name=org.gnome.keyring.SystemPrompter
Exec=$VENV/bin/gcr-tty-prompter-client
EOF
chmod 0644 "$SERVICE"

# $XDG_DATA_HOME/dbus-1/services is not necessarily monitored by
# dbus-daemon; request a service configuration reload.
if command -v busctl >/dev/null 2>&1; then
  busctl --user call \
    org.freedesktop.DBus / org.freedesktop.DBus ReloadConfig \
    >/dev/null 2>&1 || true
fi

cat <<EOF
Installed.

1. Check libgcr:
   $BIN_DIR/gcr-tty-prompter-selftest

2. Start the foreground TTY server:
   $BIN_DIR/gcr-tty-prompter-server

3. In another terminal, check the current D-Bus owner:
   busctl --user status org.gnome.keyring.SystemPrompter

Switch without uninstalling:
   $BIN_DIR/gcr-tty-prompter-mode disable   # use system/default prompter
   $BIN_DIR/gcr-tty-prompter-mode enable    # use TTY prompter again
   $BIN_DIR/gcr-tty-prompter-mode status

The mode command safely terminates only recognized gcr-prompter owners.
Unknown owners are never killed automatically.
EOF
