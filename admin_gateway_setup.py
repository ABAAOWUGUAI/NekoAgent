#!/usr/bin/env python3
"""Interactive-only setup/rotation for the public Admin password hash."""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path
import sys

from admin_gateway_security import (
    DEFAULT_SCRYPT_N,
    GatewayStateStore,
    PasswordHashStore,
    rotate_password,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Interactively set the public Admin credential hash")
    parser.add_argument("--password-hash", type=Path, required=True, help="destination hash file")
    parser.add_argument("--state-db", type=Path, required=True, help="Gateway state database")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not sys.stdin.isatty():
        print("interactive TTY required", file=sys.stderr)
        return 2
    first = getpass.getpass("New public Admin password: ")
    second = getpass.getpass("Confirm public Admin password: ")
    if first != second:
        print("password confirmation mismatch", file=sys.stderr)
        return 2
    try:
        rotate_password(
            PasswordHashStore(args.password_hash),
            GatewayStateStore(args.state_db),
            first,
            n=DEFAULT_SCRYPT_N,
        )
    except (OSError, ValueError):
        print("public Admin password setup failed", file=sys.stderr)
        return 2
    print("public Admin password hash updated; active public sessions revoked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
