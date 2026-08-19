from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from dbus_next import Message, MessageType
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType

BUS_NAME = "org.gnome.keyring.SystemPrompter"
DBUS_DEST = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
DBUS_IFACE = "org.freedesktop.DBus"


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))


def app_dir() -> Path:
    return data_home() / "gcr-tty-prompter"


def service_dir() -> Path:
    return data_home() / "dbus-1/services"


def service_path() -> Path:
    return service_dir() / f"{BUS_NAME}.service"


def disabled_service_path() -> Path:
    return app_dir() / f"{BUS_NAME}.service.disabled"


def installed_client() -> Path:
    # mode.py normally runs from the private venv installed by
    # install-user.sh. Prefer the sibling console script.
    sibling = Path(sys.executable).parent / "gcr-tty-prompter-client"
    if sibling.exists():
        return sibling

    fallback = Path.home() / ".local/bin/gcr-tty-prompter-client"
    return fallback


def service_text(client: Path) -> str:
    return (
        "[D-BUS Service]\n"
        f"Name={BUS_NAME}\n"
        f"Exec={client}\n"
    )


def is_our_service(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return (
        f"Name={BUS_NAME}" in text
        and "gcr-tty-prompter-client" in text
    )


@dataclass
class Owner:
    unique_name: str
    pid: int
    command: str
    kind: str


async def dbus_call(
    bus: MessageBus,
    member: str,
    signature: str = "",
    body: list[object] | None = None,
):
    reply = await bus.call(
        Message(
            destination=DBUS_DEST,
            path=DBUS_PATH,
            interface=DBUS_IFACE,
            member=member,
            signature=signature,
            body=body or [],
        )
    )
    if reply is None:
        raise RuntimeError(f"{member}: no D-Bus reply")
    if reply.message_type == MessageType.ERROR:
        detail = reply.body[0] if reply.body else reply.error_name
        raise RuntimeError(f"{member}: {detail}")
    return reply


async def reload_config(bus: MessageBus) -> None:
    await dbus_call(bus, "ReloadConfig")


async def get_name_owner(bus: MessageBus) -> str | None:
    reply = await bus.call(
        Message(
            destination=DBUS_DEST,
            path=DBUS_PATH,
            interface=DBUS_IFACE,
            member="GetNameOwner",
            signature="s",
            body=[BUS_NAME],
        )
    )
    if reply is None:
        return None
    if reply.message_type == MessageType.ERROR:
        if reply.error_name == "org.freedesktop.DBus.Error.NameHasNoOwner":
            return None
        detail = reply.body[0] if reply.body else reply.error_name
        raise RuntimeError(f"GetNameOwner: {detail}")
    return str(reply.body[0])


async def get_owner_pid(bus: MessageBus, unique_name: str) -> int:
    reply = await dbus_call(
        bus,
        "GetConnectionUnixProcessID",
        "s",
        [unique_name],
    )
    return int(reply.body[0])


def process_uid(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/status").read_text(
            encoding="utf-8", errors="replace"
        )
    except (FileNotFoundError, PermissionError):
        return None

    for line in text.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            if len(parts) >= 2:
                return int(parts[1])
    return None


def process_argv(pid: int) -> list[str]:
    try:
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError):
        return []
    return [
        part.decode("utf-8", errors="replace")
        for part in data.split(b"\0")
        if part
    ]


def classify_argv(argv: list[str]) -> tuple[str, str]:
    command = " ".join(argv) if argv else "<unavailable>"

    if any(
        "gcr-tty-prompter-client" in arg
        or "gcr_tty_prompter.client" in arg
        for arg in argv
    ):
        return "custom", command

    for arg in argv:
        base = Path(arg).name
        if base == "gcr-prompter":
            return "system", command

    return "unknown", command


async def owner_info(bus: MessageBus) -> Owner | None:
    unique = await get_name_owner(bus)
    if unique is None:
        return None

    pid = await get_owner_pid(bus, unique)
    kind, command = classify_argv(process_argv(pid))
    return Owner(unique, pid, command, kind)


async def wait_owner_change(
    bus: MessageBus,
    old_unique_name: str,
) -> bool:
    for _ in range(30):
        await asyncio.sleep(0.1)
        current = await get_name_owner(bus)
        if current != old_unique_name:
            return True
    return False


async def stop_owner_if_safe(
    bus: MessageBus,
    owner: Owner | None,
    *,
    expected_kind: str,
) -> bool:
    if owner is None or owner.kind != expected_kind:
        return False

    uid = process_uid(owner.pid)
    if uid != os.getuid():
        raise RuntimeError(
            f"refusing to signal pid {owner.pid}: uid {uid} != {os.getuid()}"
        )

    os.kill(owner.pid, signal.SIGTERM)
    changed = await wait_owner_change(bus, owner.unique_name)
    if not changed:
        raise RuntimeError(
            f"pid {owner.pid} did not release {BUS_NAME} after SIGTERM"
        )
    return True


def write_enabled_service() -> Path:
    target = service_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    app_dir().mkdir(parents=True, exist_ok=True)

    client = installed_client()
    if not client.exists():
        raise RuntimeError(
            f"replacement client not found: {client}\n"
            "Run install-user.sh first."
        )

    target.write_text(service_text(client), encoding="utf-8")
    os.chmod(target, 0o644)

    # Keep a copy only as state/documentation. It is outside dbus-1/services,
    # so D-Bus will never activate it while disabled.
    disabled_service_path().write_text(
        service_text(client), encoding="utf-8"
    )
    os.chmod(disabled_service_path(), 0o644)
    return target


def disable_enabled_service() -> bool:
    target = service_path()
    if not target.exists():
        return False

    if not is_our_service(target):
        raise RuntimeError(
            f"refusing to remove unmanaged service file: {target}"
        )

    app_dir().mkdir(parents=True, exist_ok=True)
    disabled_service_path().write_text(
        target.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    os.chmod(disabled_service_path(), 0o644)
    target.unlink()
    return True


async def command_disable(bus: MessageBus) -> int:
    before = await owner_info(bus)

    removed = disable_enabled_service()
    await reload_config(bus)

    stopped = await stop_owner_if_safe(
        bus, before, expected_kind="custom"
    )

    after = await owner_info(bus)

    print("Mode: system/default GCR prompter")
    print(
        "User override: disabled"
        + (" (service file removed from activation path)" if removed else "")
    )
    if stopped:
        print("Previous custom D-Bus owner: terminated")
    elif before and before.kind == "unknown":
        print(
            "Warning: current owner was not recognized and was not terminated:"
        )
        print(f"  PID {before.pid}: {before.command}")
    elif before and before.kind == "system":
        print("Current owner was already the system gcr-prompter.")

    if after is None:
        print("D-Bus owner: none; the system prompter will activate on demand.")
    else:
        print(
            f"D-Bus owner: {after.kind} PID={after.pid} {after.command}"
        )
    return 0


async def command_enable(bus: MessageBus) -> int:
    before = await owner_info(bus)

    target = write_enabled_service()
    await reload_config(bus)

    stopped = await stop_owner_if_safe(
        bus, before, expected_kind="system"
    )

    after = await owner_info(bus)

    print("Mode: gcr-tty-prompter")
    print(f"User override: enabled at {target}")
    if stopped:
        print("Previous system D-Bus owner: terminated")
    elif before and before.kind == "unknown":
        print(
            "Warning: current owner was not recognized and was not terminated:"
        )
        print(f"  PID {before.pid}: {before.command}")
    elif before and before.kind == "custom":
        print("Current owner was already gcr-tty-prompter.")

    if after is None:
        print(
            "D-Bus owner: none; gcr-tty-prompter-client will activate on demand."
        )
    else:
        print(
            f"D-Bus owner: {after.kind} PID={after.pid} {after.command}"
        )

    print(
        "Remember to keep gcr-tty-prompter-server running in the target TTY."
    )
    return 0


async def command_status(bus: MessageBus) -> int:
    target = service_path()
    enabled = target.exists() and is_our_service(target)
    owner = await owner_info(bus)

    print(
        "Configured mode: "
        + ("gcr-tty-prompter" if enabled else "system/default")
    )
    print(f"User service override: {'enabled' if enabled else 'disabled'}")
    print(f"Service path: {target}")

    if target.exists() and not enabled:
        print("Warning: a non-managed service file exists at that path.")

    if owner is None:
        print("Current D-Bus owner: none")
    else:
        print(f"Current D-Bus owner: {owner.kind}")
        print(f"Owner unique name: {owner.unique_name}")
        print(f"Owner PID: {owner.pid}")
        print(f"Owner command: {owner.command}")
    return 0


async def amain(args: argparse.Namespace) -> int:
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    try:
        if args.command == "enable":
            return await command_enable(bus)
        if args.command == "disable":
            return await command_disable(bus)
        if args.command == "status":
            return await command_status(bus)
        raise RuntimeError(f"unknown command: {args.command}")
    finally:
        bus.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Switch org.gnome.keyring.SystemPrompter between "
            "gcr-tty-prompter and the system default without uninstalling."
        )
    )
    parser.add_argument(
        "command",
        choices=("enable", "disable", "status"),
    )
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(amain(args)))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
