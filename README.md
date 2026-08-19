# gcr-tty-prompter

Experimental Python replacement for the GCR system prompter.

The design is deliberately split:

```text
GcrSystemPrompt / application
          |
          | session D-Bus
          v
+-----------------------------+
| gcr-tty-prompter-client     |
| no TTY required             |
| owns:                       |
| org.gnome.keyring.          |
|   SystemPrompter            |
+-------------+---------------+
              |
              | Unix socket
              | properties + GcrSecretExchange strings
              | (never plaintext password)
              v
+-----------------------------+
| gcr-tty-prompter-server     |
| foreground, owns /dev/tty   |
| GcrSecretExchange state     |
| Password: ********          |
+-----------------------------+
```

## Why SecretExchange lives in the TTY server

The straightforward design would read the password in the TTY server and
send the plaintext password over the Unix socket to the D-Bus client.

This implementation avoids that.

The `GcrSecretExchange` object is kept in the foreground TTY server. The
D-Bus client only forwards the exchange strings between GCR and the TTY
server. Therefore the plaintext password never crosses the bridge socket
and is never present in the D-Bus client.

The Python TTY server still handles the password in process memory, so this
is not equivalent to GCR's secure-memory guarantees.

## Requirements

- Linux
- Python 3.11+
- `dbus-next`
- a GCR runtime exporting `gcr_secret_exchange_*`
  - `libgcr-4.so.4`, or
  - older `libgcr-base-3.so.1`

Check the GCR runtime with:

```sh
ldconfig -p | grep -E 'libgcr-(4|base-3)'
```

## Install

```sh
./install-user.sh
```

This creates a private virtualenv below:

```text
~/.local/share/gcr-tty-prompter/venv
```

and installs:

```text
~/.local/share/dbus-1/services/
  org.gnome.keyring.SystemPrompter.service
```

The installer calls `org.freedesktop.DBus.ReloadConfig` when `busctl` is
available.

## Self-test

```sh
gcr-tty-prompter-selftest
```

Expected:

```text
GcrSecretExchange self-test: OK
```

## Development run

First start the TTY side in the terminal in which prompts should appear:

```sh
gcr-tty-prompter-server -v
```

It opens `/dev/tty` and creates:

```text
$XDG_RUNTIME_DIR/gcr-tty-prompter/prompter.sock
```

The directory is mode `0700`, the socket is mode `0600`, and both sides
verify Linux `SO_PEERCRED` to require the same UID.

Then run the D-Bus side manually:

```sh
gcr-tty-prompter-client -v
```

The client requests this session-bus name:

```text
org.gnome.keyring.SystemPrompter
```

If the original `gcr-prompter` already owns that name, the client exits
instead of queueing.

Inspect the current owner:

```sh
busctl --user status org.gnome.keyring.SystemPrompter
```

Once the old owner is gone, the user D-Bus activation file can start the
replacement client automatically.

## Supported GCR wire protocol

Object:

```text
/org/gnome/keyring/Prompter
```

Interface:

```text
org.gnome.keyring.internal.Prompter
```

Methods:

```text
BeginPrompting(o callback)
PerformPrompt(o callback, s type, a{sv} properties, s exchange)
StopPrompting(o callback)
```

Callback interface:

```text
org.gnome.keyring.internal.Prompter.Callback
```

Callbacks:

```text
PromptReady(s reply, a{sv} properties, s exchange)
PromptDone()
```

Prompt types implemented:

- `password`
- `confirm`

Prompt properties recognized:

- `caller-window` (ignored by the TTY UI)
- `cancel-label`
- `choice-chosen`
- `choice-label`
- `continue-label`
- `description`
- `message`
- `password-new`
- `password-strength` (display logic not implemented)
- `title`
- `warning`

For `password-new=true`, the server asks for the password twice.

For `choice-label`, the server asks a separate yes/no question and returns
`choice-chosen`.

## Cancellation

While a password/confirmation prompt is active:

- `Ctrl-C` cancels that prompt without terminating the foreground server.
- `Ctrl-D` also cancels.
- `StopPrompting()` cancels the client RPC; the server notices the Unix
  socket disconnect and restores the terminal settings.

Outside an active prompt, normal `Ctrl-C` terminates the foreground server.

## Security notes

1. The Unix socket is restricted to the same UID with filesystem
   permissions and `SO_PEERCRED`.
2. The plaintext password is not sent over the Unix socket.
3. Prompt text is treated as untrusted and C0/C1 terminal control
   characters (including ESC) are escaped before display.
4. The password is accumulated in a mutable `bytearray` and overwritten
   after it is given to `GcrSecretExchange`.
5. Python and ctypes cannot provide the same non-pageable/secure-memory
   guarantees as native GCR. This implementation should therefore be
   considered experimental for security-sensitive production use.
6. The GCR prompter D-Bus interface is explicitly an internal interface
   and may change between GCR releases.

## Architecture notes

The client implements "single prompter" queueing semantics: only one GCR
prompt callback is active at a time. A single callback may issue multiple
related prompts before `StopPrompting()`.

`GcrSecretExchange` state is also maintained for the full callback
lifetime, so successive `PerformPrompt()` calls continue the same exchange.

## Uninstall

```sh
./uninstall-user.sh
```
