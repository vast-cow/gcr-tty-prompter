#!/bin/sh
set -eu

DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
APP_DIR="$DATA_HOME/gcr-tty-prompter"
SERVICE="$DATA_HOME/dbus-1/services/org.gnome.keyring.SystemPrompter.service"
BIN_DIR="$HOME/.local/bin"

rm -f "$SERVICE"
rm -f "$BIN_DIR/gcr-tty-prompter-client"
rm -f "$BIN_DIR/gcr-tty-prompter-server"
rm -f "$BIN_DIR/gcr-tty-prompter-selftest"
rm -f "$BIN_DIR/gcr-tty-prompter-mode"
rm -f "$BIN_DIR/gcr-keyring-unlock"
rm -f "$BIN_DIR/gcr-keyring-lock"
rm -rf "$APP_DIR"

if command -v busctl >/dev/null 2>&1; then
  busctl --user call \
    org.freedesktop.DBus / org.freedesktop.DBus ReloadConfig \
    >/dev/null 2>&1 || true
fi

echo "Uninstalled. The system gcr-prompter can be activated again."
