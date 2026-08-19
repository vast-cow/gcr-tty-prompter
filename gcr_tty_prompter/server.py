from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
import socket
import stat
import termios
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import (
    PROTOCOL_VERSION,
    bridge_socket_path,
    ensure_private_dir,
    read_json_line,
    require_same_uid,
    sanitize_terminal_text,
    write_json_line,
)
from .secret_exchange import SecretExchange

LOG = logging.getLogger("gcr-tty-prompter-server")


class PromptCancelled(Exception):
    pass


def wipe(buf: bytearray | None) -> None:
    if buf is None:
        return
    for i in range(len(buf)):
        buf[i] = 0


class TTY:
    def __init__(self) -> None:
        self.fd = os.open("/dev/tty", os.O_RDWR | os.O_CLOEXEC)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def write(self, text: str) -> None:
        os.write(self.fd, text.encode("utf-8", errors="replace"))

    async def _read_some(self) -> bytes:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()

        def ready() -> None:
            if future.done():
                return
            try:
                data = os.read(self.fd, 4096)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(data)

        loop.add_reader(self.fd, ready)
        try:
            return await future
        finally:
            loop.remove_reader(self.fd)

    def _raw_attrs(self) -> tuple[list[Any], list[Any]]:
        old = termios.tcgetattr(self.fd)
        new = termios.tcgetattr(self.fd)
        new[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
        new[6][termios.VMIN] = 1
        new[6][termios.VTIME] = 0
        return old, new

    @staticmethod
    def _pop_utf8(buf: bytearray) -> None:
        if not buf:
            return
        # Remove the last UTF-8 codepoint from the byte buffer.
        buf.pop()
        while buf and (buf[-1] & 0xC0) == 0x80:
            buf.pop()
        if buf and (buf[-1] & 0xC0) == 0xC0:
            buf.pop()

    async def read_secret(self, prompt: str) -> bytearray:
        self.write(prompt)
        old, new = self._raw_attrs()
        termios.tcsetattr(self.fd, termios.TCSANOW, new)
        data = bytearray()
        try:
            while True:
                chunk = await self._read_some()
                if not chunk:
                    raise PromptCancelled()

                for byte in chunk:
                    if byte in (10, 13):  # LF / CR
                        self.write("\n")
                        return data
                    if byte in (3, 4):  # Ctrl-C / Ctrl-D
                        self.write("^C\n")
                        raise PromptCancelled()
                    if byte in (8, 127):  # Backspace / DEL
                        self._pop_utf8(data)
                        continue
                    if byte == 21:  # Ctrl-U
                        wipe(data)
                        data.clear()
                        continue
                    if byte < 0x20:
                        continue
                    # Bytes >= 0x80 must be preserved: they can be part of
                    # a UTF-8 password.
                    data.append(byte)
        except BaseException:
            if isinstance(data, bytearray):
                wipe(data)
            raise
        finally:
            termios.tcsetattr(self.fd, termios.TCSANOW, old)

    async def read_yes_no(
        self, prompt: str, *, default: bool = False
    ) -> bool:
        suffix = " [Y/n] " if default else " [y/N] "
        self.write(prompt + suffix)
        old, new = self._raw_attrs()
        termios.tcsetattr(self.fd, termios.TCSANOW, new)
        try:
            while True:
                chunk = await self._read_some()
                if not chunk:
                    raise PromptCancelled()
                for byte in chunk:
                    if byte in (3, 4):
                        self.write("^C\n")
                        raise PromptCancelled()
                    if byte in (10, 13):
                        self.write(("y" if default else "n") + "\n")
                        return default
                    if byte in (ord("y"), ord("Y")):
                        self.write("y\n")
                        return True
                    if byte in (ord("n"), ord("N")):
                        self.write("n\n")
                        return False
        finally:
            termios.tcsetattr(self.fd, termios.TCSANOW, old)


@dataclass
class ServerSession:
    token: str
    exchange: SecretExchange
    last_used: float
    prompt_task: asyncio.Task[Any] | None = None


class PrompterServer:
    def __init__(self, tty: TTY) -> None:
        self.tty = tty
        self.sessions: dict[str, ServerSession] = {}
        self.ui_lock = asyncio.Lock()

    def _render(self, prompt_type: str, props: dict[str, Any]) -> None:
        safe = sanitize_terminal_text
        self.tty.write("\n")
        title = props.get("title")
        message = props.get("message")
        description = props.get("description")
        warning = props.get("warning")

        if title:
            self.tty.write(f"== {safe(title)} ==\n")
        if message:
            self.tty.write(f"{safe(message)}\n")
        else:
            self.tty.write("Authentication required\n")
        if description:
            self.tty.write(f"{safe(description)}\n")
        if warning:
            self.tty.write(f"WARNING: {safe(warning)}\n")

        if prompt_type == "confirm":
            cont = safe(props.get("continue-label") or "Continue")
            cancel = safe(props.get("cancel-label") or "Cancel")
            self.tty.write(f"{cont} / {cancel}\n")

    async def _choice_properties(
        self, props: dict[str, Any]
    ) -> dict[str, Any]:
        label = props.get("choice-label")
        if not label:
            return {}
        current = bool(props.get("choice-chosen", False))
        chosen = await self.tty.read_yes_no(
            sanitize_terminal_text(label), default=current
        )
        return {"choice-chosen": chosen}

    async def _prompt_password(
        self, props: dict[str, Any]
    ) -> tuple[str, bytearray | None, dict[str, Any]]:
        self._render("password", props)

        password_new = bool(props.get("password-new", False))
        while True:
            first = await self.tty.read_secret("Password: ")
            if not password_new:
                break

            second: bytearray | None = None
            try:
                second = await self.tty.read_secret("Repeat password: ")
                if hmac.compare_digest(first, second):
                    break
                self.tty.write("Passwords do not match. Try again.\n")
            finally:
                wipe(second)

            wipe(first)

        try:
            changed = await self._choice_properties(props)
            return "yes", first, changed
        except BaseException:
            wipe(first)
            raise

    async def _prompt_confirm(
        self, props: dict[str, Any]
    ) -> tuple[str, None, dict[str, Any]]:
        self._render("confirm", props)
        changed = await self._choice_properties(props)

        cont = sanitize_terminal_text(
            props.get("continue-label") or "Continue"
        )
        cancel = sanitize_terminal_text(
            props.get("cancel-label") or "Cancel"
        )
        yes = await self.tty.read_yes_no(
            f"{cont}? ({cancel} = no)", default=False
        )
        return ("yes" if yes else "no"), None, changed

    async def _run_prompt(
        self, prompt_type: str, props: dict[str, Any]
    ) -> tuple[str, bytearray | None, dict[str, Any]]:
        async with self.ui_lock:
            if prompt_type == "password":
                return await self._prompt_password(props)
            if prompt_type == "confirm":
                return await self._prompt_confirm(props)
            raise ValueError(f"unsupported prompt type: {prompt_type}")

    async def _handle_begin(self, req: dict[str, Any]) -> dict[str, Any]:
        token = str(req.get("session", ""))
        if not token:
            raise ValueError("missing session token")
        if token in self.sessions:
            raise ValueError("session already exists")

        exchange = SecretExchange()
        try:
            initial = exchange.begin()
        except BaseException:
            exchange.close()
            raise

        self.sessions[token] = ServerSession(
            token=token,
            exchange=exchange,
            last_used=time.monotonic(),
        )
        return {"ok": True, "exchange": initial}

    async def _handle_stop(self, req: dict[str, Any]) -> dict[str, Any]:
        token = str(req.get("session", ""))
        session = self.sessions.pop(token, None)
        if session is not None:
            if (
                session.prompt_task is not None
                and not session.prompt_task.done()
            ):
                session.prompt_task.cancel()
            session.exchange.close()
        return {"ok": True}

    async def _handle_prompt(
        self,
        req: dict[str, Any],
        reader: asyncio.StreamReader,
    ) -> dict[str, Any] | None:
        token = str(req.get("session", ""))
        session = self.sessions.get(token)
        if session is None:
            raise ValueError("unknown session")

        prompt_type = str(req.get("type", ""))
        if prompt_type not in ("password", "confirm"):
            raise ValueError("invalid prompt type")

        received = req.get("exchange")
        if not isinstance(received, str):
            raise ValueError("missing secret exchange")
        if not session.exchange.receive(received):
            raise ValueError("invalid GcrSecretExchange input")

        props = req.get("properties", {})
        if not isinstance(props, dict):
            raise ValueError("properties must be an object")

        session.last_used = time.monotonic()
        ui_task = asyncio.create_task(
            self._run_prompt(prompt_type, props)
        )
        session.prompt_task = ui_task

        # If the D-Bus client cancels, it closes this RPC connection. Detect
        # that and restore the terminal immediately.
        disconnected = asyncio.create_task(reader.read(1))
        try:
            done, _pending = await asyncio.wait(
                {ui_task, disconnected},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnected in done and ui_task not in done:
                ui_task.cancel()
                try:
                    await ui_task
                except asyncio.CancelledError:
                    pass
                return None

            disconnected.cancel()
            try:
                await disconnected
            except asyncio.CancelledError:
                pass

            try:
                reply, secret, changed = await ui_task
            except PromptCancelled:
                reply, secret, changed = "no", None, {}

            try:
                sent = session.exchange.send(
                    secret if reply == "yes" and prompt_type == "password"
                    else None
                )
            finally:
                wipe(secret)

            session.last_used = time.monotonic()
            return {
                "ok": True,
                "reply": reply,
                "properties": changed,
                "exchange": sent,
            }
        finally:
            if session.prompt_task is ui_task:
                session.prompt_task = None
            disconnected.cancel()

    async def handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            sock = writer.get_extra_info("socket")
            if sock is None:
                raise PermissionError("cannot inspect peer")
            require_same_uid(sock)

            req = await read_json_line(reader)
            if req.get("version") != PROTOCOL_VERSION:
                raise ValueError("unsupported bridge protocol version")

            op = req.get("op")
            if op == "begin":
                response = await self._handle_begin(req)
            elif op == "stop":
                response = await self._handle_stop(req)
            elif op == "prompt":
                response = await self._handle_prompt(req, reader)
                if response is None:
                    return
            else:
                raise ValueError(f"unknown operation: {op!r}")

            await write_json_line(writer, response)
        except asyncio.CancelledError:
            raise
        except (BrokenPipeError, ConnectionResetError, EOFError):
            pass
        except Exception as exc:
            LOG.warning("bridge request failed: %s", exc)
            try:
                await write_json_line(
                    writer, {"ok": False, "error": str(exc)}
                )
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def reap_stale_sessions(self) -> None:
        while True:
            await asyncio.sleep(300)
            now = time.monotonic()
            stale = [
                token
                for token, session in self.sessions.items()
                if session.prompt_task is None
                and now - session.last_used > 3600
            ]
            for token in stale:
                session = self.sessions.pop(token, None)
                if session is not None:
                    session.exchange.close()

    async def close(self) -> None:
        for session in list(self.sessions.values()):
            if session.prompt_task and not session.prompt_task.done():
                session.prompt_task.cancel()
            session.exchange.close()
        self.sessions.clear()


def prepare_socket(path: Path) -> socket.socket:
    ensure_private_dir(path.parent)

    try:
        st = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if st.st_uid != os.getuid():
            raise RuntimeError(f"socket path not owned by current uid: {path}")
        if not stat.S_ISSOCK(st.st_mode):
            raise RuntimeError(
                f"refusing to replace non-socket path: {path}"
            )
        path.unlink()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
        os.chmod(path, 0o600)
        sock.listen(16)
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


async def amain(args: argparse.Namespace) -> None:
    path = bridge_socket_path()
    tty = TTY()
    prompter = PrompterServer(tty)
    sock = prepare_socket(path)
    reaper: asyncio.Task[Any] | None = None

    try:
        server = await asyncio.start_unix_server(
            prompter.handle_connection, sock=sock
        )
        tty.write(
            f"gcr-tty-prompter server ready\n"
            f"socket: {sanitize_terminal_text(path)}\n"
            f"Ctrl-C at an active prompt cancels that prompt.\n"
        )
        reaper = asyncio.create_task(prompter.reap_stale_sessions())
        async with server:
            await server.serve_forever()
    finally:
        if reaper is not None:
            reaper.cancel()
        await prompter.close()
        tty.close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Foreground TTY server for gcr-tty-prompter"
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


if __name__ == "__main__":
    main()
