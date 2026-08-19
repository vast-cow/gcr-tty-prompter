from __future__ import annotations

import ctypes
import ctypes.util


def _load_library(candidates: list[str]) -> ctypes.CDLL:
    errors: list[str] = []
    for candidate in candidates:
        resolved = ctypes.util.find_library(candidate)
        names = [resolved] if resolved else []
        names.append(candidate)
        for name in names:
            if not name:
                continue
            try:
                return ctypes.CDLL(name)
            except OSError as exc:
                errors.append(f"{name}: {exc}")
    raise RuntimeError(
        "Could not load libgcr. Tried: " + "; ".join(errors)
    )


_gcr = _load_library([
    "gcr-4",
    "libgcr-4.so.4",
    "gcr-base-3",
    "libgcr-base-3.so.1",
])
_glib = _load_library(["glib-2.0", "libglib-2.0.so.0"])
_gobject = _load_library(["gobject-2.0", "libgobject-2.0.so.0"])

_gcr.gcr_secret_exchange_new.argtypes = [ctypes.c_char_p]
_gcr.gcr_secret_exchange_new.restype = ctypes.c_void_p

_gcr.gcr_secret_exchange_begin.argtypes = [ctypes.c_void_p]
_gcr.gcr_secret_exchange_begin.restype = ctypes.c_void_p

_gcr.gcr_secret_exchange_receive.argtypes = [
    ctypes.c_void_p,
    ctypes.c_char_p,
]
_gcr.gcr_secret_exchange_receive.restype = ctypes.c_int

# Use void* for the secret so a mutable bytearray can be passed directly.
_gcr.gcr_secret_exchange_send.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_ssize_t,
]
_gcr.gcr_secret_exchange_send.restype = ctypes.c_void_p

_glib.g_free.argtypes = [ctypes.c_void_p]
_glib.g_free.restype = None

_gobject.g_object_unref.argtypes = [ctypes.c_void_p]
_gobject.g_object_unref.restype = None


def _take_gchar(ptr: int | None) -> str:
    if not ptr:
        raise RuntimeError("libgcr returned NULL")
    try:
        return ctypes.string_at(ptr).decode("utf-8")
    finally:
        _glib.g_free(ptr)


class SecretExchange:
    """Small ctypes wrapper around GcrSecretExchange."""

    def __init__(self) -> None:
        self._ptr = _gcr.gcr_secret_exchange_new(None)
        if not self._ptr:
            raise RuntimeError("gcr_secret_exchange_new() failed")

    def close(self) -> None:
        ptr = self._ptr
        if ptr:
            self._ptr = None
            _gobject.g_object_unref(ptr)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def begin(self) -> str:
        if not self._ptr:
            raise RuntimeError("SecretExchange is closed")
        return _take_gchar(_gcr.gcr_secret_exchange_begin(self._ptr))

    def receive(self, exchange: str) -> bool:
        if not self._ptr:
            raise RuntimeError("SecretExchange is closed")
        return bool(
            _gcr.gcr_secret_exchange_receive(
                self._ptr, exchange.encode("utf-8")
            )
        )

    def send(self, secret: bytearray | None) -> str:
        if not self._ptr:
            raise RuntimeError("SecretExchange is closed")

        if secret is None:
            secret_ptr = None
            secret_len = 0
        elif len(secret) == 0:
            # A stable non-NULL pointer distinguishes empty secret from NULL.
            empty = ctypes.c_char(b"\0")
            secret_ptr = ctypes.addressof(empty)
            secret_len = 0
        else:
            first = ctypes.c_char.from_buffer(secret)
            secret_ptr = ctypes.addressof(first)
            secret_len = len(secret)

        return _take_gchar(
            _gcr.gcr_secret_exchange_send(
                self._ptr, secret_ptr, secret_len
            )
        )
