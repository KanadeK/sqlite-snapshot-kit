# sqlite-snapshot-kit

Create a self-contained SQLite snapshot, verify it after copying, and rehearse a restore into
a **new file**. One offline CLI; Python's standard library does the work.

The online backup includes committed WAL data. Verification checks SHA-256, SQLite integrity,
foreign keys, schema metadata and per-table row counts. Optional table row minimums catch a
missing or unexpectedly empty application table. The restore command verifies the result too.

## Install

Requires Python 3.12+ with sqlite3 and its defensive-configuration API.
Use a maintained Python/SQLite distribution with current vendor security patches.
No third-party runtime dependencies, sqlite3 executable, Docker, service or API key is needed.
From a source checkout:

```sh
python -m venv .venv
# Linux/macOS:
. .venv/bin/activate
# Windows PowerShell, instead:
# .\.venv\Scripts\Activate.ps1
python -m pip install .
sqlite-snapshot-kit --version
```

If PowerShell activation is restricted, invoke `.venv\Scripts\python.exe` and
`.venv\Scripts\sqlite-snapshot-kit.exe` directly. This is a source installation;
no PyPI publication is claimed.

## Run the complete example

```sh
python examples/demo.py
```

The example creates a synthetic database, keeps a WAL writer open, creates and verifies a
snapshot, restores it, then SELECTs the actual restored row. All files live in a temporary
directory and are cleaned up. It requires no pre-existing database.

## Use with your database

```sh
sqlite-snapshot-kit create app.sqlite3 backup-001 --min-rows notes=1
sqlite-snapshot-kit verify backup-001 --min-rows notes=1
sqlite-snapshot-kit restore backup-001 restored.sqlite3
# Also available: python -m sqlite_snapshot_kit ...
```

The source must be an initialized, unencrypted SQLite 3 file. `backup-001` must not exist;
its parent must already exist. `restored.sqlite3` and its WAL/SHM/journal sidecars must not exist.
The commands refuse overwrites. A restore is useful as a drill or as a candidate replacement;
stop your application and manage its switch to the new file separately.

Successful commands print JSON with `ok`, bytes, SHA-256, table count and total rows.
Restore adds `restored: true`. No row contents or source paths are printed.

| Exit | Meaning |
|---:|---|
| 0 | Operation completed and all requested checks passed |
| 1 | Verification failed: digest/schema/count/integrity/FK mismatch, invalid snapshot or row minimum |
| 2 | Invalid arguments, resource budget, filesystem error or SQLite operational failure |

Use `--min-rows TABLE=COUNT` repeatedly. Counts are nonnegative; a requested table must exist
even for COUNT=0. Duplicate table requirements use the highest minimum. Table names are data,
not executed SQL. These checks do not replace your application's own restore smoke tests.

`--timeout SECONDS` defaults to 30 and accepts up to 86400. `--max-bytes N` defaults to
4294967296 (4 GiB). Deadlines are cooperative, not a hard process timeout. Long integrity checks
and row counts may need a larger budget. Locked or unsupported databases may fail sooner.

## Snapshot format

```text
backup-001/
  database.sqlite3
  manifest.json
```

The manifest records tool/SQLite versions, creation time, payload name/size/SHA-256,
user_version, application_id, a schema fingerprint and user-table names/counts. It contains
no original file path or row values. Table names and counts may still be private.

Keep those two files together. Extra files, sidecars, symlinks/reparse points, duplicate JSON
keys and unknown format fields cause verification failure. Verification opens the standalone
snapshot as immutable/read-only and hashes it before and after inspection. Do not modify it
concurrently. The format has strict versioning; future formats will need explicit support.

## Limits and alternatives

This version does not schedule, encrypt, upload, migrate or provide point-in-time recovery.
The manifest is **not signed**: modifying both files can produce another valid snapshot.
SHA-256 detects mismatch against the supplied manifest, not who created it. The database copy
contains all application data; protect the directory with appropriate filesystem permissions.

Virtual tables (including FTS and RTree), custom SQLite extensions/collations, SQLCipher and application-level invariants are outside
the support contract. Initialized empty databases are supported; zero-byte uninitialized files
are refused. SQL statements above 1,048,576 characters, schema inventories over 16 MiB,
more than 10,000 user tables and manifests over 2 MiB are refused. SQLite defensive mode is
enabled on source, target and verifier connections. Virtual-table databases are rejected
before integrity checks or user-table queries. See [design](docs/DESIGN.md) for exact rules.

SQLite's own `.backup` is a good option when you already maintain verification scripts.
[RestoreVerify](https://github.com/RestoreVerify/restoreverify) handles broader database restore
workflows; [sqlite-vault](https://github.com/suhlig/sqlite-vault) adds encrypted remote backups.
This project narrows the workflow to local snapshots with an independent Python implementation,
no external database client and an explicit no-overwrite restore drill.

## Cleanup and development

`python -m pip uninstall sqlite-snapshot-kit` removes the installed package. Remove the virtual
environment with your file manager when finished. Snapshots and restored databases remain
until you deliberately delete them. No cache, background worker or retention cleanup runs.
An interrupted create may leave an incomplete directory; verify it before use and remove it
manually if invalid. A failed cleanup can also leave a partial new restore file.

```sh
python -m unittest discover -s tests -v
```

See [CONTRIBUTING](CONTRIBUTING.md), [SECURITY](SECURITY.md) and [MIT license](LICENSE).
The read-only CI configuration targets Windows/Ubuntu/macOS with Python 3.12/3.13;
remote CI success must be established by an actual published run.

## 中文说明

这是一个要求 Python 3.12+、仅依赖标准库的离线 SQLite 快照工具，提供创建、验证、恢复到新文件三个命令。
在线备份会包含已提交的 WAL 数据；验证检查摘要、SQLite 完整性、外键、表结构和行数。
`--min-rows notes=1` 可要求指定表存在且达到最小行数。恢复后还会重新检查新数据库。

先运行 `python examples/demo.py` 体验完整流程。实际使用示例：
`sqlite-snapshot-kit create app.sqlite3 backup-001`，然后执行 `verify backup-001` 和
`restore backup-001 restored.sqlite3`。输出目录与文件必须是新路径，不覆盖生产数据库。

快照包含全部数据库数据，清单也包含表名和行数。首版拒绝 FTS、RTree 等虚拟表数据库，
并显式启用 defensive 模式。它不提供加密、签名、调度或增量恢复。
退出码 0 表示成功，1 表示验证失败，2 表示参数、预算或操作错误。卸载使用
`python -m pip uninstall sqlite-snapshot-kit`，快照和恢复文件由使用者自行清理。
