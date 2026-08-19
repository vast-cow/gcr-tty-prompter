from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Any

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType

SERVICE_NAME = "org.freedesktop.secrets"
SERVICE_PATH = "/org/freedesktop/secrets"
SERVICE_IFACE = "org.freedesktop.Secret.Service"
PROMPT_IFACE = "org.freedesktop.Secret.Prompt"
COLLECTION_IFACE = "org.freedesktop.Secret.Collection"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

NO_OBJECT = "/"


class SecretServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class OperationResult:
    collection: str
    changed: tuple[str, ...]
    prompted: bool
    dismissed: bool
    locked: bool


async def _proxy_interface(
    bus: MessageBus,
    destination: str,
    path: str,
    interface: str,
):
    introspection = await bus.introspect(destination, path)
    proxy = bus.get_proxy_object(destination, path, introspection)
    return proxy.get_interface(interface)


async def service_interface(bus: MessageBus):
    return await _proxy_interface(
        bus, SERVICE_NAME, SERVICE_PATH, SERVICE_IFACE
    )


async def collection_locked(
    bus: MessageBus,
    collection: str,
) -> bool:
    properties = await _proxy_interface(
        bus, SERVICE_NAME, collection, PROPERTIES_IFACE
    )
    value = await properties.call_get(COLLECTION_IFACE, "Locked")
    if not isinstance(value, Variant):
        raise SecretServiceError(
            "Collection.Locked did not return a D-Bus variant"
        )
    return bool(value.value)


async def resolve_collection(
    bus: MessageBus,
    *,
    alias: str = "default",
    path: str | None = None,
) -> str:
    if path is not None:
        if not path.startswith("/"):
            raise SecretServiceError(
                f"collection path must be a D-Bus object path: {path!r}"
            )
        if path == NO_OBJECT:
            raise SecretServiceError("collection path '/' means no object")
        return path

    service = await service_interface(bus)
    collection = await service.call_read_alias(alias)
    if collection == NO_OBJECT:
        raise SecretServiceError(
            f"Secret Service alias {alias!r} does not exist"
        )
    return collection


def _variant_object_paths(value: Any) -> tuple[str, ...]:
    if isinstance(value, Variant):
        value = value.value

    if value is None:
        return ()

    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)

    # Prompt results for Unlock/Lock are specified as ao. Be strict enough
    # to notice backend/protocol mismatches without failing on an empty
    # result represented by a variant.
    raise SecretServiceError(
        f"unexpected prompt result type: {type(value).__name__}"
    )


async def run_prompt(
    bus: MessageBus,
    prompt_path: str,
    *,
    window_id: str = "",
) -> tuple[bool, tuple[str, ...]]:
    if prompt_path == NO_OBJECT:
        return False, ()

    prompt = await _proxy_interface(
        bus, SERVICE_NAME, prompt_path, PROMPT_IFACE
    )
    loop = asyncio.get_running_loop()
    completed: asyncio.Future[tuple[bool, tuple[str, ...]]] = (
        loop.create_future()
    )

    def on_completed(dismissed: bool, result: Variant) -> None:
        if completed.done():
            return
        try:
            paths = _variant_object_paths(result)
        except Exception as exc:
            completed.set_exception(exc)
        else:
            completed.set_result((bool(dismissed), paths))

    prompt.on_completed(on_completed)
    try:
        # Subscribe to Completed before invoking Prompt() so a very fast
        # backend cannot race the signal listener.
        await prompt.call_prompt(window_id)
        return await completed
    finally:
        try:
            prompt.off_completed(on_completed)
        except Exception:
            pass


async def change_lock_state(
    *,
    lock: bool,
    alias: str = "default",
    path: str | None = None,
    window_id: str = "",
) -> OperationResult:
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    try:
        collection = await resolve_collection(
            bus, alias=alias, path=path
        )
        service = await service_interface(bus)

        if lock:
            immediate, prompt_path = await service.call_lock([collection])
        else:
            immediate, prompt_path = await service.call_unlock([collection])

        changed = tuple(str(item) for item in immediate)
        prompted = prompt_path != NO_OBJECT
        dismissed = False

        if prompted:
            dismissed, from_prompt = await run_prompt(
                bus, prompt_path, window_id=window_id
            )
            changed = tuple(dict.fromkeys((*changed, *from_prompt)))

        locked = await collection_locked(bus, collection)

        return OperationResult(
            collection=collection,
            changed=changed,
            prompted=prompted,
            dismissed=dismissed,
            locked=locked,
        )
    finally:
        bus.disconnect()


def _parser(action: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"gcr-keyring-{action}",
        description=(
            f"Request the Secret Service to {action} a keyring collection."
        ),
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--alias",
        default="default",
        help=(
            "Secret Service collection alias (default: %(default)s). "
            "Useful values commonly include 'default' and 'session'."
        ),
    )
    target.add_argument(
        "--path",
        help=(
            "Explicit collection D-Bus object path instead of an alias, "
            "for example /org/freedesktop/secrets/collection/login"
        ),
    )
    parser.add_argument(
        "--window-id",
        default="",
        help=(
            "Window identifier passed to org.freedesktop.Secret.Prompt. "
            "The empty string is appropriate for this TTY-oriented tool."
        ),
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="print only errors",
    )
    return parser


async def _amain(action: str, args: argparse.Namespace) -> int:
    result = await change_lock_state(
        lock=(action == "lock"),
        alias=args.alias,
        path=args.path,
        window_id=args.window_id,
    )

    if result.dismissed:
        if not args.quiet:
            print(f"{action}: prompt dismissed", file=sys.stderr)
        return 2

    desired_locked = action == "lock"
    success = result.locked == desired_locked
    if not args.quiet:
        prompt_text = " (prompted)" if result.prompted else ""
        if success:
            state = "locked" if result.locked else "unlocked"
            print(
                f"{action}: OK{prompt_text}: {result.collection} "
                f"is {state}"
            )
        else:
            state = "locked" if result.locked else "unlocked"
            print(
                f"{action}: request completed but {result.collection} "
                f"is still {state}{prompt_text}",
                file=sys.stderr,
            )

    return 0 if success else 1


def main_for(action: str) -> None:
    parser = _parser(action)
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_amain(action, args)))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"{action}: error: {exc}", file=sys.stderr)
        raise SystemExit(1)


def unlock_main() -> None:
    main_for("unlock")


def lock_main() -> None:
    main_for("lock")


if __name__ == "__main__":
    # Intended entry points are unlock_main() and lock_main().
    raise SystemExit(
        "run gcr-keyring-unlock or gcr-keyring-lock"
    )
