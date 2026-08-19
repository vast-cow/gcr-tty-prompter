from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import struct
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 1024 * 1024


class BridgeError(RuntimeError):
    pass


def runtime_dir() -> Path:
    value = os.environ.get("XDG_RUNTIME_DIR")
    if value:
        return Path(value)

    fallback = Path(f"/run/user/{os.getuid()}")
    if fallback.is_dir():
        return fallback

    raise RuntimeError(
        "XDG_RUNTIME_DIR is not set and /run/user/<uid> does not exist"
    )


def bridge_dir() -> Path:
    override = os.environ.get("GCR_TTY_PROMPTER_DIR")
    if override:
        return Path(override)
    return runtime_dir() / "gcr-tty-prompter"


def bridge_socket_path() -> Path:
    override = os.environ.get("GCR_TTY_PROMPTER_SOCKET")
    if override:
        return Path(override)
    return bridge_dir() / "prompter.sock"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"not a directory: {path}")
    if st.st_uid != os.getuid():
        raise RuntimeError(f"directory not owned by current uid: {path}")
    os.chmod(path, 0o700)


def peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    if not hasattr(socket, "SO_PEERCRED"):
        raise RuntimeError("SO_PEERCRED is required (Linux only)")
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    return struct.unpack("3i", raw)


def require_same_uid(sock: socket.socket) -> None:
    _pid, uid, _gid = peer_credentials(sock)
    if uid != os.getuid():
        raise PermissionError(f"peer uid {uid} != current uid {os.getuid()}")


async def read_json_line(reader: asyncio.StreamReader) -> dict[str, Any]:
    data = await reader.readline()
    if not data:
        raise EOFError("peer closed connection")
    if len(data) > MAX_MESSAGE_BYTES:
        raise BridgeError("bridge message too large")
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise BridgeError("bridge message must be a JSON object")
    return obj


async def write_json_line(
    writer: asyncio.StreamWriter, obj: dict[str, Any]
) -> None:
    data = json.dumps(
        obj, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    if len(data) > MAX_MESSAGE_BYTES:
        raise BridgeError("bridge message too large")
    writer.write(data)
    await writer.drain()


async def bridge_rpc(
    payload: dict[str, Any],
    *,
    connect_timeout: float = 2.0,
    reply_timeout: float | None = None,
) -> dict[str, Any]:
    payload = dict(payload)
    payload["version"] = PROTOCOL_VERSION

    path = bridge_socket_path()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path=str(path)),
            timeout=connect_timeout,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise BridgeError(f"TTY server unavailable at {path}: {exc}") from exc

    try:
        sock = writer.get_extra_info("socket")
        if sock is None:
            raise BridgeError("could not inspect Unix socket peer")
        require_same_uid(sock)

        await write_json_line(writer, payload)

        if reply_timeout is None:
            response = await read_json_line(reader)
        else:
            response = await asyncio.wait_for(
                read_json_line(reader), timeout=reply_timeout
            )

        if response.get("ok") is not True:
            raise BridgeError(str(response.get("error", "bridge request failed")))
        return response
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


def sanitize_terminal_text(value: object) -> str:
    """Render untrusted prompt text without permitting terminal escapes."""
    text = str(value)
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch in ("\n", "\t"):
            out.append(ch)
        elif code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    return "".join(out)
