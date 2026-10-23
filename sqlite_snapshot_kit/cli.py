import argparse
import json
import math
import sqlite3
import sys

from . import __version__
from .core import (
    MAX_BYTES,
    SnapshotError,
    VerificationError,
    create_snapshot,
    restore_snapshot,
    verify_snapshot,
)


def positive(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 2**63 - 1:
        raise argparse.ArgumentTypeError("must be between 1 and 2^63-1")
    return number


def seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0 < number <= 86_400:
        raise argparse.ArgumentTypeError("must be finite and between 0 and 86400")
    return number


def minimum(value: str) -> tuple[str, int]:
    try:
        name, raw = value.rsplit("=", 1)
        count = int(raw)
        if not name or count < 0:
            raise ValueError
        return name, count
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected TABLE=NONNEGATIVE_COUNT") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create, verify and rehearse restoring local SQLite snapshots."
    )
    parser.add_argument("--version", action="version", version=f"sqlite-snapshot-kit {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "verify", "restore"):
        command = commands.add_parser(name)
        command.add_argument("source", help="SQLite file for create; snapshot directory otherwise")
        if name != "verify":
            command.add_argument(
                "destination", help="new directory for create; new file for restore"
            )
        command.add_argument("--timeout", type=seconds, default=30)
        command.add_argument("--max-bytes", type=positive, default=MAX_BYTES)
        command.add_argument("--min-rows", type=minimum, action="append", default=[])
    args = parser.parse_args(argv)
    counts = {}
    for name, count in args.min_rows:
        counts[name] = max(count, counts.get(name, 0))
    options = {"timeout": args.timeout, "max_bytes": args.max_bytes, "minimum_rows": counts}
    try:
        if args.command == "create":
            result = create_snapshot(args.source, args.destination, **options)
        elif args.command == "verify":
            result = verify_snapshot(args.source, **options)
        else:
            result = restore_snapshot(args.source, args.destination, **options)
    except VerificationError as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileExistsError:
        print("error: destination exists; choose a new directory/file", file=sys.stderr)
        return 2
    except sqlite3.Error:
        print(
            "error: SQLite operation failed; database may be locked, corrupt, unsupported or over deadline",
            file=sys.stderr,
        )
        return 2
    except OSError:
        print(
            "error: filesystem operation failed; check paths, space and permissions",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0
