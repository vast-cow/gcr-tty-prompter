from __future__ import annotations

from .secret_exchange import SecretExchange


def main() -> None:
    # Verify that the installed libgcr can complete a protocol-1 exchange.
    a = SecretExchange()
    b = SecretExchange()
    try:
        first = a.begin()
        assert b.receive(first)

        secret = bytearray(b"selftest-password")
        try:
            second = b.send(secret)
        finally:
            for i in range(len(secret)):
                secret[i] = 0

        assert a.receive(second)
        print("GcrSecretExchange self-test: OK")
    finally:
        a.close()
        b.close()


if __name__ == "__main__":
    main()
