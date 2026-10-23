# Design and acceptance criteria

Users: maintainers of local SQLite applications who need a small, offline backup/restore drill.
MVP: online-backup a live database into a fresh directory, verify its independent snapshot,
then restore into a new file and query the result. No scheduler, server or database CLI required.

## Create

The source must exist and have the SQLite 3 header; opening uses an escaped file URI with
mode=ro, so a misspelled path cannot become an empty database. The Online Backup API includes
committed WAL state. A fresh directory is exclusively reserved; existing destinations fail.
The target is changed to DELETE journal mode and closed, producing a standalone file.
Integrity, foreign keys and optional row minimums are checked before a manifest is written.
No source path or row values are included in the manifest.

The directory contains exactly database.sqlite3 and manifest.json. A crash may leave an
incomplete directory, which verify refuses. Failure cleanup only attempts known filenames
inside the directory created by this call; it never recursively removes arbitrary contents.
An incomplete directory may remain if cleanup fails. The tool does not promise an atomic
directory publication or crash-proof storage durability.

## Verify

The manifest is limited to 2 MiB and uses strict format version 1, fixed payload filename,
exact keys, unique table names, nonnegative integer counts and SHA-256 digests. Duplicate JSON
keys and links/reparse points are refused. Extra files, including WAL/SHM/journal sidecars,
invalidate a standalone snapshot. The payload is hashed before and after inspecting it.

Python 3.12+ is required so SQLITE_DBCONFIG_DEFENSIVE can be enabled and read back on every
source, target and verifier connection. A schema metadata pass rejects virtual tables
(rootpage=0) before integrity checks and table queries; FTS/RTree are outside this release.
Inspection opens immutable=1 with mode=ro, query_only=ON and trusted_schema=OFF. This avoids
writing sidecars for the snapshot, and assumes the input stays unchanged. Hashes detect many
concurrent modifications but are not a lock or defense against a hostile concurrent writer.
Do not change the snapshot while using this tool. SQL never comes from the manifest.
Table identifiers from sqlite_schema are quoted; row minimums are looked up as data.

The schema fingerprint is SHA-256 over rows sorted by type/name/table name, each encoded as
a compact ensure_ascii JSON array of type/name/table-name/SQL plus newline. Schema statements
over 1,048,576 characters, encoded schema inventories above 16 MiB, more than 10,000 user
tables and manifests above 2 MiB are refused. SQLite internal sqlite_* tables are excluded
from row counts. Virtual tables and databases requiring custom modules are not supported.

PRAGMA integrity_check(20) must return only ok. PRAGMA foreign_key_check must return no rows.
user_version, application_id, schema fingerprint and per-table row counts must match the manifest.
A coherent but logically wrong database may still pass; optional table minimums are only a
small application-level assertion. The tool cannot validate all business invariants.

## Restore

The snapshot is fully verified before opening the output with exclusive creation. Existing
files, dangling symlinks and orphan target sidecars are refused. Bytes are copied with a digest,
fsynced, then independently inspected. Failure attempts to remove only the new output file
with the same file identity. Switching a live application to that file is an operator action.

Timeouts are cooperative checks in backup callbacks, SQLite progress handlers and streaming
I/O; they are not process-level hard deadlines. A permanently locked source may fail sooner
than the overall timeout. Default payload budget is 4 GiB and timeout is 30 seconds per operation.

## Acceptance

- Include committed active WAL rows and exclude an uncommitted transaction.
- Restore the exact records, user_version and application_id into a new queryable file.
- Reject digest mismatch, truncated payload, corrupt b-tree even with an updated digest,
  foreign-key violation, strict-manifest errors and missing/extra snapshot files.
- Never overwrite an existing source, destination or target sidecar.
- Clean failed newly-created output when possible; bound locked-database and metadata work.
- Demonstrate real installed-package create/verify/restore, including a SELECT against the restore.

Security boundaries: no encryption, authenticity signature, PITR, migration or arbitrary SQL
assertion execution. Anyone who can rewrite the payload and manifest together can create a new
valid snapshot. Backups contain all database data, and manifests disclose table names/counts.
Use trusted local paths and maintain permissions. A maintained Python/SQLite build is required;
this application is not a native SQLite parser sandbox. Virtual tables, custom extensions/collations and encrypted
databases may not work and are outside the 0.1.0 support contract.

References: [Online Backup API](https://sqlite.org/backup.html),
[PRAGMA integrity_check](https://sqlite.org/pragma.html#pragma_integrity_check),
[read-only WAL](https://sqlite.org/wal.html#read_only_databases).
Code and synthetic fixtures were written independently. No third-party backup implementation
or database was copied. SQLite is supplied through Python's standard-library sqlite3 module.
