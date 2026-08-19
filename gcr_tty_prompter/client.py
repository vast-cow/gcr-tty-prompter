from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
from collections import deque
from dataclasses import dataclass
from typing import Any

from dbus_next import Message, MessageType, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import (
    BusType,
    NameFlag,
    RequestNameReply,
)

from .common import BridgeError, bridge_rpc

LOG = logging.getLogger("gcr-tty-prompter-client")

BUS_NAME = "org.gnome.keyring.SystemPrompter"
OBJECT_PATH = "/org/gnome/keyring/Prompter"
PROMPTER_IFACE = "org.gnome.keyring.internal.Prompter"
CALLBACK_IFACE = "org.gnome.keyring.internal.Prompter.Callback"

INTROSPECTION_XML = """\
<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.gnome.keyring.internal.Prompter">
    <method name="BeginPrompting">
      <arg name="callback" type="o" direction="in"/>
    </method>
    <method name="PerformPrompt">
      <arg name="callback" type="o" direction="in"/>
      <arg name="type" type="s" direction="in"/>
      <arg name="properties" type="a{sv}" direction="in"/>
      <arg name="exchange" type="s" direction="in"/>
    </method>
    <method name="StopPrompting">
      <arg name="callback" type="o" direction="in"/>
    </method>
  </interface>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect">
      <arg name="xml_data" type="s" direction="out"/>
    </method>
  </interface>
  <interface name="org.freedesktop.DBus.Peer">
    <method name="Ping"/>
  </interface>
</node>
"""

KNOWN_PROPERTIES = {
    "caller-window",
    "cancel-label",
    "choice-chosen",
    "choice-label",
    "continue-label",
    "description",
    "message",
    "password-new",
    "password-strength",
    "title",
    "warning",
}


def unwrap_variant(value: Any) -> Any:
    if isinstance(value, Variant):
        return unwrap_variant(value.value)
    if isinstance(value, dict):
        return {
            str(k): unwrap_variant(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [unwrap_variant(v) for v in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def input_properties(values: dict[str, Variant]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values.items():
        if key in KNOWN_PROPERTIES:
            result[key] = unwrap_variant(value)
    return result


def output_properties(values: dict[str, Any]) -> dict[str, Variant]:
    result: dict[str, Variant] = {}
    if "choice-chosen" in values:
        result["choice-chosen"] = Variant(
            "b", bool(values["choice-chosen"])
        )
    return result


@dataclass
class PromptSession:
    sender: str
    callback: str
    bridge_token: str
    properties: dict[str, Any]
    active: bool = False
    ready: bool = False
    prompt_task: asyncio.Task[Any] | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.sender, self.callback)


class SystemPrompter:
    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus
        self.sessions: dict[tuple[str, str], PromptSession] = {}
        self.waiting: deque[tuple[str, str]] = deque()
        self.active_key: tuple[str, str] | None = None
        self.state_lock = asyncio.Lock()

    async def add_name_owner_match(self) -> None:
        rule = (
            "type='signal',"
            "sender='org.freedesktop.DBus',"
            "interface='org.freedesktop.DBus',"
            "member='NameOwnerChanged'"
        )
        reply = await self.bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[rule],
            )
        )
        if reply is None or reply.message_type == MessageType.ERROR:
            raise RuntimeError("could not subscribe to NameOwnerChanged")

    def message_handler(self, msg: Message) -> Message | bool | None:
        if (
            msg.message_type == MessageType.SIGNAL
            and msg.interface == "org.freedesktop.DBus"
            and msg.member == "NameOwnerChanged"
            and len(msg.body) == 3
        ):
            name, _old_owner, new_owner = msg.body
            if (
                isinstance(name, str)
                and name.startswith(":")
                and new_owner == ""
            ):
                asyncio.create_task(self._sender_vanished(name))
            return None

        if msg.message_type != MessageType.METHOD_CALL:
            return None

        if (
            msg.path == OBJECT_PATH
            and msg.interface == "org.freedesktop.DBus.Introspectable"
            and msg.member == "Introspect"
        ):
            return Message.new_method_return(
                msg, signature="s", body=[INTROSPECTION_XML]
            )

        if (
            msg.path == OBJECT_PATH
            and msg.interface == "org.freedesktop.DBus.Peer"
            and msg.member == "Ping"
        ):
            return Message.new_method_return(msg)

        if msg.path != OBJECT_PATH or msg.interface != PROMPTER_IFACE:
            return None

        if not msg.sender:
            return Message.new_error(
                msg,
                "org.freedesktop.DBus.Error.Failed",
                "missing D-Bus sender",
            )

        try:
            if msg.member == "BeginPrompting":
                return self._begin(msg)
            if msg.member == "PerformPrompt":
                return self._perform(msg)
            if msg.member == "StopPrompting":
                return self._stop(msg)
            return Message.new_error(
                msg,
                "org.freedesktop.DBus.Error.UnknownMethod",
                f"unknown method {msg.member}",
            )
        except Exception as exc:
            LOG.exception("D-Bus request failed")
            return Message.new_error(
                msg, "org.freedesktop.DBus.Error.Failed", str(exc)
            )

    def _begin(self, msg: Message) -> Message:
        if len(msg.body) != 1 or not isinstance(msg.body[0], str):
            return Message.new_error(
                msg,
                "org.freedesktop.DBus.Error.InvalidArgs",
                "BeginPrompting expects callback object path",
            )

        callback = msg.body[0]
        key = (msg.sender, callback)
        if key in self.sessions:
            return Message.new_error(
                msg,
                "org.freedesktop.DBus.Error.Failed",
                "Already begun prompting for this callback",
            )

        session = PromptSession(
            sender=msg.sender,
            callback=callback,
            bridge_token=secrets.token_urlsafe(24),
            properties={},
        )
        self.sessions[key] = session
        self.waiting.append(key)
        asyncio.create_task(self._activate_next())
        return Message.new_method_return(msg)

    def _perform(self, msg: Message) -> Message:
        if len(msg.body) != 4:
            return Message.new_error(
                msg,
                "org.freedesktop.DBus.Error.InvalidArgs",
                "PerformPrompt expects (o, s, a{sv}, s)",
            )

        callback, prompt_type, props, exchange = msg.body
        key = (msg.sender, callback)
        session = self.sessions.get(key)

        if session is None or not session.active:
            return Message.new_error(
                msg,
                "org.freedesktop.DBus.Error.Failed",
                "Not begun prompting for this callback",
            )
        if not session.ready:
            return Message.new_error(
                msg,
                "org.freedesktop.DBus.Error.Failed",
                "Already performing a prompt for this callback",
            )
        if prompt_type not in ("password", "confirm"):
            return Message.new_error(
                msg,
                "org.freedesktop.DBus.Error.InvalidArgs",
                "prompt type must be 'password' or 'confirm'",
            )
        if not isinstance(props, dict) or not isinstance(exchange, str):
            return Message.new_error(
                msg,
                "org.freedesktop.DBus.Error.InvalidArgs",
                "invalid properties or secret exchange",
            )

        session.properties.update(input_properties(props))
        session.ready = False
        session.prompt_task = asyncio.create_task(
            self._perform_async(session, prompt_type, exchange)
        )
        return Message.new_method_return(msg)

    def _stop(self, msg: Message) -> Message:
        if len(msg.body) != 1 or not isinstance(msg.body[0], str):
            return Message.new_error(
                msg,
                "org.freedesktop.DBus.Error.InvalidArgs",
                "StopPrompting expects callback object path",
            )
        key = (msg.sender, msg.body[0])
        asyncio.create_task(self._finish_session(key, send_done=True))
        return Message.new_method_return(msg)

    async def _callback_call(
        self,
        session: PromptSession,
        member: str,
        signature: str = "",
        body: list[Any] | None = None,
    ) -> None:
        reply = await self.bus.call(
            Message(
                destination=session.sender,
                path=session.callback,
                interface=CALLBACK_IFACE,
                member=member,
                signature=signature,
                body=body or [],
            )
        )
        if reply is None:
            raise RuntimeError(f"{member}: no D-Bus reply")
        if reply.message_type == MessageType.ERROR:
            detail = reply.body[0] if reply.body else reply.error_name
            raise RuntimeError(f"{member} callback failed: {detail}")

    async def _send_ready(
        self,
        session: PromptSession,
        reply: str,
        properties: dict[str, Any],
        exchange: str,
    ) -> None:
        await self._callback_call(
            session,
            "PromptReady",
            "sa{sv}s",
            [reply, output_properties(properties), exchange],
        )

    async def _activate_next(self) -> None:
        async with self.state_lock:
            if self.active_key is not None:
                return

            session: PromptSession | None = None
            while self.waiting:
                key = self.waiting.popleft()
                candidate = self.sessions.get(key)
                if candidate is not None:
                    session = candidate
                    self.active_key = key
                    session.active = True
                    break

        if session is None:
            return

        try:
            response = await bridge_rpc(
                {
                    "op": "begin",
                    "session": session.bridge_token,
                },
                reply_timeout=2.0,
            )
            exchange = response.get("exchange")
            if not isinstance(exchange, str):
                raise BridgeError("TTY server returned no secret exchange")

            # StopPrompting() may have raced with bridge session creation.
            # If so, tear the just-created bridge session down instead of
            # delivering a late PromptReady().
            if session.key not in self.sessions:
                try:
                    await bridge_rpc(
                        {
                            "op": "stop",
                            "session": session.bridge_token,
                        },
                        reply_timeout=2.0,
                    )
                except Exception:
                    pass
                return

            await self._send_ready(session, "", {}, exchange)
            if session.key not in self.sessions:
                return
            session.ready = True
            LOG.debug(
                "prompt callback ready: %s %s",
                session.sender,
                session.callback,
            )
        except Exception as exc:
            LOG.warning("could not activate TTY prompt: %s", exc)
            await self._finish_session(session.key, send_done=True)

    async def _perform_async(
        self,
        session: PromptSession,
        prompt_type: str,
        exchange: str,
    ) -> None:
        try:
            response = await bridge_rpc(
                {
                    "op": "prompt",
                    "session": session.bridge_token,
                    "type": prompt_type,
                    "properties": session.properties,
                    "exchange": exchange,
                }
            )

            reply = response.get("reply")
            returned_exchange = response.get("exchange")
            changed = response.get("properties", {})

            if reply not in ("yes", "no"):
                raise BridgeError("TTY server returned invalid reply")
            if not isinstance(returned_exchange, str):
                raise BridgeError("TTY server returned invalid exchange")
            if not isinstance(changed, dict):
                changed = {}

            session.properties.update(changed)
            await self._send_ready(
                session, reply, changed, returned_exchange
            )

            if session.key in self.sessions:
                session.ready = True
                session.prompt_task = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("prompt failed: %s", exc)
            await self._finish_session(session.key, send_done=True)

    async def _finish_session(
        self,
        key: tuple[str, str],
        *,
        send_done: bool,
    ) -> None:
        async with self.state_lock:
            session = self.sessions.pop(key, None)
            if session is None:
                return

            try:
                self.waiting.remove(key)
            except ValueError:
                pass

            was_active = self.active_key == key
            if was_active:
                self.active_key = None

            task = session.prompt_task
            if (
                task is not None
                and task is not asyncio.current_task()
                and not task.done()
            ):
                task.cancel()

        if session.active:
            try:
                await bridge_rpc(
                    {
                        "op": "stop",
                        "session": session.bridge_token,
                    },
                    reply_timeout=2.0,
                )
            except Exception as exc:
                LOG.debug("bridge stop failed: %s", exc)

        if send_done:
            try:
                await self._callback_call(session, "PromptDone")
            except Exception as exc:
                LOG.debug("PromptDone failed: %s", exc)

        if was_active:
            await self._activate_next()

    async def _sender_vanished(self, sender: str) -> None:
        keys = [
            key for key in list(self.sessions)
            if key[0] == sender
        ]
        for key in keys:
            await self._finish_session(key, send_done=False)

    async def close(self) -> None:
        keys = list(self.sessions)
        for key in keys:
            await self._finish_session(key, send_done=False)


async def amain(args: argparse.Namespace) -> None:
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    prompter = SystemPrompter(bus)
    bus.add_message_handler(prompter.message_handler)
    await prompter.add_name_owner_match()

    result = await bus.request_name(
        BUS_NAME, flags=NameFlag.DO_NOT_QUEUE
    )
    if result not in (
        RequestNameReply.PRIMARY_OWNER,
        RequestNameReply.ALREADY_OWNER,
    ):
        raise RuntimeError(
            f"{BUS_NAME} is already owned by another process ({result.name})"
        )

    LOG.info(
        "owning %s as %s", BUS_NAME, bus.unique_name
    )
    try:
        await bus.wait_for_disconnect()
    finally:
        await prompter.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="D-Bus client/bridge replacing gcr-prompter"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="verbose logging"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        LOG.error("%s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
