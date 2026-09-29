#!/usr/bin/env python3
"""Generate the 256-bit Gateway-to-Bridge credential without displaying it."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import sys
import tempfile


def generate_service_token(path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise FileExistsError("service_token_already_exists")
    token = secrets.token_urlsafe(32) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(token.encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the private Admin Gateway service credential")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        generate_service_token(args.output)
    except OSError:
        print("service credential setup failed", file=sys.stderr)
        return 2
    print("service credential created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
