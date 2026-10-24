"""Local SQLite online snapshots with strict manifests and no-overwrite restore drills."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

PAYLOAD = "database.sqlite3"
MANIFEST = "manifest.json"
MAX_BYTES = 4 * 1024**3
MAX_MANIFEST = 2 * 1024 * 1024
SIDECARS = ("-wal", "-shm", "-journal")


class SnapshotError(ValueError):
    """Input/operational error (CLI exit 2)."""


class VerificationError(SnapshotError):
    """A snapshot or database failed verification (CLI exit 1)."""


def _options(timeout: float, max_bytes: int, minimum_rows: dict | None) -> tuple[float, dict]:
    if type(timeout) not in (int, float) or not 0 < timeout <= 86_400 or not math.isfinite(timeout):
        raise SnapshotError("timeout must be finite and between 0 and 86400 seconds")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 2**63 - 1:
        raise SnapshotError("max_bytes must be between 1 and 2^63-1")
    minimum_rows = {} if minimum_rows is None else minimum_rows
    if not isinstance(minimum_rows, dict) or any(
        not isinstance(name, str) or not name or type(count) is not int or count < 0
        for name, count in minimum_rows.items()
    ):
        raise SnapshotError("minimum_rows must map nonempty table names to nonnegative integers")
    return time.monotonic() + timeout, minimum_rows


def _deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise SnapshotError("operation deadline exceeded")


def _regular(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _no_sidecars(path: Path) -> bool:
    return not any(os.path.lexists(str(path) + suffix) for suffix in SIDECARS)


def _connect(path: Path, deadline: float, *, immutable: bool = False) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=0.1)
    try:
        _defensive(connection)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    except BaseException:
        connection.close()
        raise
    return connection


def _defensive(connection: sqlite3.Connection) -> None:
    if not hasattr(connection, "setconfig") or not hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
        raise SnapshotError("Python 3.12+ with SQLite defensive configuration is required")
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    if not connection.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE):
        raise SnapshotError("SQLite defensive mode could not be enabled")


def _inspect(path: Path, deadline: float, minimum_rows: dict) -> dict:
    with closing(_connect(path, deadline, immutable=True)) as connection:
        _deadline(deadline)
        fingerprint = hashlib.sha256()
        schema_bytes = 0
        names = []
        with closing(
            connection.execute(
                "SELECT type, name, tbl_name, CASE WHEN length(sql)>1048576 THEN NULL ELSE sql END, length(sql), rootpage "
                "FROM sqlite_schema ORDER BY type, name, tbl_name"
            )
        ) as cursor:
            for kind, name, table_name, sql, sql_length, rootpage in cursor:
                _deadline(deadline)
                if kind == "table" and rootpage == 0:
                    raise SnapshotError("virtual tables are unsupported in version 0.1.0")
                if sql_length is not None and sql_length > 1_048_576:
                    raise SnapshotError("schema statement exceeds one million characters")
                encoded = (
                    json.dumps(
                        (kind, name, table_name, sql), ensure_ascii=True, separators=(",", ":")
                    ).encode()
                    + b"\n"
                )
                schema_bytes += len(encoded)
                if schema_bytes > 16 * 1024 * 1024:
                    raise SnapshotError("schema inventory exceeds 16 MiB")
                fingerprint.update(encoded)
                if kind == "table" and not name.startswith("sqlite_"):
                    names.append(name)
                    if len(names) > 10_000:
                        raise SnapshotError("database exceeds 10000 user tables")
        if connection.execute("PRAGMA integrity_check(20)").fetchall() != [("ok",)]:
            raise VerificationError("SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise VerificationError("SQLite foreign-key violations found")
        tables = []
        for name in names:
            _deadline(deadline)
            quoted = '"' + name.replace('"', '""') + '"'
            count = connection.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
            tables.append({"name": name, "rows": count})
        counts = {item["name"]: item["rows"] for item in tables}
        for name, minimum in minimum_rows.items():
            if name not in counts or counts[name] < minimum:
                raise VerificationError("required table missing or below the requested row minimum")
        return {
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "application_id": connection.execute("PRAGMA application_id").fetchone()[0],
            "schema_sha256": fingerprint.hexdigest(),
            "tables": tables,
        }


def _hash(path: Path, deadline: float, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            _deadline(deadline)
            size += len(chunk)
            if size > max_bytes:
                raise SnapshotError("payload exceeds max_bytes")
            digest.update(chunk)
    return size, digest.hexdigest()


def _exclusive(path: Path):
    # Windows O_EXCL can follow a dangling link; check the directory entry itself.
    if os.path.lexists(path):
        raise FileExistsError("destination already exists")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o600), "wb")


def _summary(manifest: dict) -> dict:
    return {
        "ok": True,
        "bytes": manifest["file"]["bytes"],
        "sha256": manifest["file"]["sha256"],
        "tables": len(manifest["database"]["tables"]),
        "rows": sum(table["rows"] for table in manifest["database"]["tables"]),
    }


def _cleanup_owned_folder(folder: Path, identity: tuple[int, int]) -> None:
    """Only a newly reserved directory and known files; never recurse through arbitrary data."""
    try:
        status = folder.lstat()
        if not stat.S_ISDIR(status.st_mode) or (status.st_dev, status.st_ino) != identity:
            return
        for name in [PAYLOAD, MANIFEST, *(PAYLOAD + suffix for suffix in SIDECARS)]:
            path = folder / name
            if os.path.lexists(path) and _regular(path):
                path.unlink()
        folder.rmdir()  # Unexpected files keep the directory intact.
    except OSError:
        pass  # Preserve incomplete output when cleanup is impossible; verification refuses it.


def create_snapshot(
    source: str | Path,
    destination: str | Path,
    *,
    timeout: float = 30,
    max_bytes: int = MAX_BYTES,
    minimum_rows: dict | None = None,
) -> dict:
    deadline, minimum_rows = _options(timeout, max_bytes, minimum_rows)
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise SnapshotError("source must be an existing SQLite file")
    with source.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise SnapshotError("source must be an initialized unencrypted SQLite 3 database")
    destination.mkdir(mode=0o700)
    identity = (destination.stat().st_dev, destination.stat().st_ino)
    payload = destination / PAYLOAD
    try:
        with _exclusive(payload):
            pass
        with (
            closing(_connect(source, deadline)) as original,
            closing(sqlite3.connect(payload)) as target,
        ):
            _defensive(target)
            page_size = original.execute("PRAGMA page_size").fetchone()[0]

            def progress(status: int, remaining: int, total: int):
                _deadline(deadline)
                if total * page_size > max_bytes:
                    raise SnapshotError("online backup exceeds max_bytes")

            original.backup(target, pages=256, progress=progress, sleep=0.01)
            target.execute("PRAGMA journal_mode=DELETE")
        if not _no_sidecars(payload):
            raise VerificationError("snapshot is not a standalone database")
        metadata = _inspect(payload, deadline, minimum_rows)
        size, digest = _hash(payload, deadline, max_bytes)
        manifest = {
            "format_version": 1,
            "tool_version": __version__,
            "sqlite_version": sqlite3.sqlite_version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "file": {"name": PAYLOAD, "bytes": size, "sha256": digest},
            "database": metadata,
        }
        encoded = (
            json.dumps(manifest, ensure_ascii=True, indent=2, allow_nan=False) + "\n"
        ).encode()
        if len(encoded) > MAX_MANIFEST:
            raise SnapshotError("schema inventory exceeds the manifest byte limit")
        with _exclusive(destination / MANIFEST) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return _summary(manifest)
    except BaseException:
        _cleanup_owned_folder(destination, identity)
        raise


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VerificationError("manifest has duplicate JSON keys")
        result[key] = value
    return result


def _load_manifest(folder: Path) -> dict:
    if folder.is_symlink() or not folder.is_dir():
        raise VerificationError("snapshot must be an existing nonsymlink directory")
    status = folder.lstat()
    if getattr(status, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise VerificationError("snapshot directory cannot be a reparse point")
    if {path.name for path in folder.iterdir()} != {PAYLOAD, MANIFEST}:
        raise VerificationError("snapshot must contain exactly database.sqlite3 and manifest.json")
    if not all(_regular(folder / name) for name in (PAYLOAD, MANIFEST)):
        raise VerificationError("snapshot files must be regular files, not links or reparse points")
    try:
        with (folder / MANIFEST).open("rb") as stream:
            data = stream.read(MAX_MANIFEST + 1)
        if len(data) > MAX_MANIFEST:
            raise VerificationError("manifest exceeds the byte limit")
        manifest = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
        if not isinstance(manifest, dict) or set(manifest) != {
            "format_version",
            "tool_version",
            "sqlite_version",
            "created_at",
            "file",
            "database",
        }:
            raise ValueError
        if type(manifest["format_version"]) is not int or manifest["format_version"] != 1:
            raise ValueError
        if not all(
            isinstance(manifest[key], str)
            for key in ("tool_version", "sqlite_version", "created_at")
        ):
            raise ValueError
        file = manifest["file"]
        database = manifest["database"]
        if (
            not isinstance(file, dict)
            or set(file) != {"name", "bytes", "sha256"}
            or file["name"] != PAYLOAD
        ):
            raise ValueError
        if type(file["bytes"]) is not int or file["bytes"] < 1 or not _is_hash(file["sha256"]):
            raise ValueError
        if not isinstance(database, dict) or set(database) != {
            "user_version",
            "application_id",
            "schema_sha256",
            "tables",
        }:
            raise ValueError
        if any(
            type(database[key]) is not int for key in ("user_version", "application_id")
        ) or not _is_hash(database["schema_sha256"]):
            raise ValueError
        tables = database["tables"]
        if not isinstance(tables, list) or len(tables) > 100_000:
            raise ValueError
        seen = set()
        for table in tables:
            if not isinstance(table, dict) or set(table) != {"name", "rows"}:
                raise ValueError
            name, rows = table["name"], table["rows"]
            if (
                not isinstance(name, str)
                or not name
                or name in seen
                or type(rows) is not int
                or rows < 0
            ):
                raise ValueError
            seen.add(name)
        return manifest
    except (ValueError, KeyError, TypeError, RecursionError, UnicodeError) as exc:
        if isinstance(exc, VerificationError):
            raise
        raise VerificationError("invalid or unsupported snapshot manifest") from exc


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _verify(folder: Path, deadline: float, max_bytes: int, minimum_rows: dict) -> dict:
    manifest = _load_manifest(folder)
    payload = folder / PAYLOAD
    if payload.stat().st_size > max_bytes:
        raise SnapshotError("payload exceeds max_bytes")
    if not _no_sidecars(payload):
        raise VerificationError("snapshot has SQLite sidecars")
    actual = _hash(payload, deadline, max_bytes)
    expected = (manifest["file"]["bytes"], manifest["file"]["sha256"])
    if actual != expected:
        raise VerificationError("snapshot size or SHA-256 mismatch")
    try:
        metadata = _inspect(payload, deadline, minimum_rows)
    except sqlite3.Error as exc:
        _deadline(deadline)
        raise VerificationError(
            "SQLite validation failed; database is corrupt or requires unavailable schema features"
        ) from exc
    if metadata != manifest["database"]:
        raise VerificationError("schema, table counts or database metadata differ from manifest")
    if _hash(payload, deadline, max_bytes) != expected:
        raise VerificationError("snapshot changed during verification")
    return manifest


def verify_snapshot(
    snapshot: str | Path,
    *,
    timeout: float = 30,
    max_bytes: int = MAX_BYTES,
    minimum_rows: dict | None = None,
) -> dict:
    deadline, minimum_rows = _options(timeout, max_bytes, minimum_rows)
    return _summary(_verify(Path(snapshot), deadline, max_bytes, minimum_rows))


def restore_snapshot(
    snapshot: str | Path,
    output: str | Path,
    *,
    timeout: float = 30,
    max_bytes: int = MAX_BYTES,
    minimum_rows: dict | None = None,
) -> dict:
    deadline, minimum_rows = _options(timeout, max_bytes, minimum_rows)
    snapshot, output = Path(snapshot), Path(output)
    manifest = _verify(snapshot, deadline, max_bytes, minimum_rows)
    if not _no_sidecars(output):
        raise SnapshotError("restore target has existing SQLite sidecars")
    identity = None
    try:
        digest, size = hashlib.sha256(), 0
        with _exclusive(output) as target:
            status = os.fstat(target.fileno())
            identity = (status.st_dev, status.st_ino)
            with (snapshot / PAYLOAD).open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    _deadline(deadline)
                    size += len(chunk)
                    if size > max_bytes:
                        raise SnapshotError("restore exceeds max_bytes")
                    digest.update(chunk)
                    target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if (size, digest.hexdigest()) != (manifest["file"]["bytes"], manifest["file"]["sha256"]):
            raise VerificationError("snapshot changed during restore")
        if (
            not _no_sidecars(output)
            or _inspect(output, deadline, minimum_rows) != manifest["database"]
        ):
            raise VerificationError("restored database differs from the verified snapshot")
        return {**_summary(manifest), "restored": True}
    except BaseException:
        if identity is not None:
            try:
                status = output.lstat()
                if stat.S_ISREG(status.st_mode) and (status.st_dev, status.st_ino) == identity:
                    output.unlink()
            except OSError:
                pass
        raise
