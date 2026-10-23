import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from sqlite_snapshot_kit import core
from sqlite_snapshot_kit.core import (
    SnapshotError,
    VerificationError,
    create_snapshot,
    restore_snapshot,
    verify_snapshot,
)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "source.sqlite3"
        self.snapshot = self.root / "snapshot"
        self.restored = self.root / "restored.sqlite3"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
            connection.executemany("INSERT INTO notes(body) VALUES (?)", [("first",), ("second",)])
            connection.execute("PRAGMA user_version=7")
            connection.execute("PRAGMA application_id=42")
            connection.commit()

    def manifest(self):
        return json.loads((self.snapshot / core.MANIFEST).read_text(encoding="utf-8"))

    def write_manifest(self, manifest):
        (self.snapshot / core.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

    def update_digest(self):
        manifest = self.manifest()
        data = (self.snapshot / core.PAYLOAD).read_bytes()
        manifest["file"].update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        self.write_manifest(manifest)

    def test_create_verify_restore_and_real_query(self):
        before = self.database.read_bytes()
        result = create_snapshot(self.database, self.snapshot, minimum_rows={"notes": 2})
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result, verify_snapshot(self.snapshot))
        self.assertTrue(restore_snapshot(self.snapshot, self.restored)["restored"])
        with closing(sqlite3.connect(self.restored)) as connection:
            self.assertEqual(
                connection.execute("SELECT id, body FROM notes ORDER BY id").fetchall(),
                [(1, "first"), (2, "second")],
            )
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone(), (7,))
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone(), (42,))
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual({p.name for p in self.snapshot.iterdir()}, {core.PAYLOAD, core.MANIFEST})

    def test_active_wal_committed_and_uncommitted_data(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO notes(body) VALUES ('committed in WAL')")
            writer.commit()
            writer.execute("INSERT INTO notes(body) VALUES ('uncommitted')")
            self.assertTrue(Path(str(self.database) + "-wal").exists())
            create_snapshot(self.database, self.snapshot, minimum_rows={"notes": 3})
            writer.rollback()
        restore_snapshot(self.snapshot, self.restored)
        with closing(sqlite3.connect(self.restored)) as connection:
            values = [row[0] for row in connection.execute("SELECT body FROM notes")]
        self.assertIn("committed in WAL", values)
        self.assertNotIn("uncommitted", values)

    def test_standalone_snapshot_has_delete_journal_mode(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO notes(body) VALUES ('WAL')")
            writer.commit()
            create_snapshot(self.database, self.snapshot)
        with closing(sqlite3.connect(self.snapshot / core.PAYLOAD)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone(), ("delete",))
        self.assertEqual({p.name for p in self.snapshot.iterdir()}, {core.PAYLOAD, core.MANIFEST})

    def test_source_with_unicode_spaces_hash_and_apostrophe(self):
        renamed = self.root / "source café # 'name.sqlite3"
        self.database.rename(renamed)
        create_snapshot(renamed, self.snapshot)
        self.assertTrue(verify_snapshot(self.snapshot)["ok"])

    @unittest.skipIf(
        sys.platform == "win32", "question marks and double quotes are invalid Windows filenames"
    )
    def test_posix_url_query_characters_in_source_path(self):
        renamed = self.root / 'source ? # "name.sqlite3'
        self.database.rename(renamed)
        create_snapshot(renamed, self.snapshot)
        self.assertTrue(verify_snapshot(self.snapshot)["ok"])

    def test_quoted_table_name_is_data_not_sql(self):
        name = 'odd"; DROP TABLE notes;--'
        with closing(sqlite3.connect(self.database)) as connection:
            quoted = '"' + name.replace('"', '""') + '"'
            connection.execute(f"CREATE TABLE {quoted}(id INTEGER)")
            connection.execute(f"INSERT INTO {quoted} VALUES(1)")
            connection.commit()
        create_snapshot(self.database, self.snapshot, minimum_rows={name: 1, "notes": 2})
        self.assertEqual(verify_snapshot(self.snapshot)["tables"], 2)

    def test_existing_snapshot_and_restore_target_never_overwritten(self):
        self.snapshot.mkdir()
        marker = self.snapshot / "keep.txt"
        marker.write_text("KEEP", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            create_snapshot(self.database, self.snapshot)
        self.assertEqual(marker.read_text(encoding="utf-8"), "KEEP")
        other = self.root / "valid"
        create_snapshot(self.database, other)
        self.restored.write_bytes(b"KEEP")
        with self.assertRaises(FileExistsError):
            restore_snapshot(other, self.restored)
        self.assertEqual(self.restored.read_bytes(), b"KEEP")

    def test_defensive_and_readonly_modes_are_enabled(self):
        with closing(core._connect(self.database, time.monotonic() + 10)) as connection:
            self.assertTrue(connection.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE))
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone(), (1,))
            self.assertEqual(connection.execute("PRAGMA trusted_schema").fetchone(), (0,))
            connection.execute("PRAGMA writable_schema=ON")
            self.assertEqual(connection.execute("PRAGMA writable_schema").fetchone(), (0,))

    def test_virtual_tables_refused_before_integrity_or_row_queries(self):
        with closing(sqlite3.connect(self.database)) as connection:
            try:
                connection.execute("CREATE VIRTUAL TABLE search USING fts5(body)")
            except sqlite3.OperationalError:
                self.skipTest("SQLite build does not include FTS5")
            connection.execute("INSERT INTO search VALUES ('synthetic')")
            connection.commit()
        with self.assertRaisesRegex(SnapshotError, "virtual tables"):
            create_snapshot(self.database, self.snapshot)
        self.assertFalse(self.snapshot.exists())

    def test_existing_restore_sidecar_refused(self):
        create_snapshot(self.database, self.snapshot)
        for suffix in core.SIDECARS:
            sidecar = Path(str(self.restored) + suffix)
            sidecar.write_bytes(b"KEEP")
            with self.assertRaises(SnapshotError):
                restore_snapshot(self.snapshot, self.restored)
            self.assertEqual(sidecar.read_bytes(), b"KEEP")
            self.assertFalse(self.restored.exists())
            sidecar.unlink()

    def test_symlinks_and_dangling_output_refused(self):
        create_snapshot(self.database, self.snapshot)
        try:
            self.restored.symlink_to(self.root / "missing")
        except OSError:
            self.skipTest("symlink creation requires OS privileges")
        with self.assertRaises(FileExistsError):
            restore_snapshot(self.snapshot, self.restored)
        self.assertFalse((self.root / "missing").exists())
        payload = self.snapshot / core.PAYLOAD
        actual = self.root / "actual.sqlite3"
        payload.rename(actual)
        payload.symlink_to(actual)
        with self.assertRaises(VerificationError):
            verify_snapshot(self.snapshot)

    def test_snapshot_folder_symlink_refused(self):
        create_snapshot(self.database, self.snapshot)
        link = self.root / "alias"
        try:
            link.symlink_to(self.snapshot, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation requires OS privileges")
        with self.assertRaises(VerificationError):
            verify_snapshot(link)

    def test_missing_source_does_not_create_database_or_output(self):
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(SnapshotError):
            create_snapshot(missing, self.snapshot)
        self.assertFalse(missing.exists())
        self.assertFalse(self.snapshot.exists())

    def test_non_sqlite_and_uninitialized_empty_file_refused(self):
        for data in [b"not a SQLite file", b""]:
            self.database.write_bytes(data)
            with self.assertRaises(SnapshotError):
                create_snapshot(self.database, self.snapshot)
            self.assertFalse(self.snapshot.exists())

    def test_initialized_empty_database_supported(self):
        self.database.unlink()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("VACUUM")
        result = create_snapshot(self.database, self.snapshot)
        self.assertEqual((result["tables"], result["rows"]), (0, 0))
        self.assertTrue(verify_snapshot(self.snapshot)["ok"])

    def test_foreign_key_violation_blocks_snapshot_and_cleans_new_output(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE children(parent INTEGER REFERENCES notes(id))")
            connection.execute("INSERT INTO children VALUES (999)")
            connection.commit()
        with self.assertRaises(VerificationError):
            create_snapshot(self.database, self.snapshot)
        self.assertFalse(self.snapshot.exists())

    def test_row_minimum_and_missing_table_gate(self):
        for minimum in [{"notes": 3}, {"missing": 0}]:
            with self.assertRaises(VerificationError):
                create_snapshot(self.database, self.snapshot, minimum_rows=minimum)
            self.assertFalse(self.snapshot.exists())
        create_snapshot(self.database, self.snapshot)
        with self.assertRaises(VerificationError):
            verify_snapshot(self.snapshot, minimum_rows={"notes": 3})
        with self.assertRaises(VerificationError):
            restore_snapshot(self.snapshot, self.restored, minimum_rows={"notes": 3})
        self.assertFalse(self.restored.exists())

    def test_payload_tampering_and_truncation_detected(self):
        create_snapshot(self.database, self.snapshot)
        payload = self.snapshot / core.PAYLOAD
        original = payload.read_bytes()
        for data in [original[:-1], original + b"extra", b"bad bytes"]:
            payload.write_bytes(data)
            with self.assertRaises(VerificationError):
                verify_snapshot(self.snapshot)
            with self.assertRaises(VerificationError):
                restore_snapshot(self.snapshot, self.restored)
            self.assertFalse(self.restored.exists())

    def test_integrity_check_runs_even_when_digest_matches(self):
        create_snapshot(self.database, self.snapshot)
        payload = self.snapshot / core.PAYLOAD
        data = bytearray(payload.read_bytes())
        data[100] = 0  # Corrupt the sqlite_schema b-tree page type.
        payload.write_bytes(data)
        self.update_digest()
        with self.assertRaises(VerificationError):
            verify_snapshot(self.snapshot)

    def test_schema_and_table_inventory_mismatch_detected(self):
        create_snapshot(self.database, self.snapshot)
        original = self.manifest()
        for mutate in [
            lambda m: m["database"]["tables"][0].update(rows=99),
            lambda m: m["database"].update(schema_sha256="0" * 64),
            lambda m: m["database"].update(user_version=999),
        ]:
            manifest = json.loads(json.dumps(original))
            mutate(manifest)
            self.write_manifest(manifest)
            with self.assertRaises(VerificationError):
                verify_snapshot(self.snapshot)

    def test_manifest_path_cannot_select_other_file(self):
        create_snapshot(self.database, self.snapshot)
        manifest = self.manifest()
        for name in ["../source.sqlite3", str(self.database), "other.sqlite3"]:
            manifest["file"]["name"] = name
            self.write_manifest(manifest)
            with self.assertRaises(VerificationError):
                verify_snapshot(self.snapshot)

    def test_duplicate_json_keys_bad_encoding_and_oversized_manifest(self):
        create_snapshot(self.database, self.snapshot)
        for data in [
            b'{"format_version":1,"format_version":1}',
            b"\xff",
            b"{",
            b"x" * (core.MAX_MANIFEST + 1),
        ]:
            (self.snapshot / core.MANIFEST).write_bytes(data)
            with self.assertRaises(VerificationError):
                verify_snapshot(self.snapshot)

    def test_strict_manifest_types_and_unknown_fields(self):
        create_snapshot(self.database, self.snapshot)
        original = self.manifest()
        for mutate in [
            lambda m: m.update(format_version=True),
            lambda m: m.update(format_version=2),
            lambda m: m.update(unknown=True),
            lambda m: m["file"].update(bytes=True),
            lambda m: m["database"]["tables"][0].update(rows=True),
            lambda m: m["database"]["tables"].append(m["database"]["tables"][0]),
        ]:
            manifest = json.loads(json.dumps(original))
            mutate(manifest)
            self.write_manifest(manifest)
            with self.assertRaises(VerificationError):
                verify_snapshot(self.snapshot)

    def test_incomplete_extra_file_and_wal_snapshot_refused(self):
        create_snapshot(self.database, self.snapshot)
        for name in ["extra.txt", core.PAYLOAD + "-wal", core.PAYLOAD + "-shm"]:
            extra = self.snapshot / name
            extra.write_bytes(b"")
            with self.assertRaises(VerificationError):
                verify_snapshot(self.snapshot)
            extra.unlink()
        (self.snapshot / core.MANIFEST).unlink()
        with self.assertRaises(VerificationError):
            verify_snapshot(self.snapshot)

    def test_byte_budgets_and_cleanup(self):
        with self.assertRaises(SnapshotError):
            create_snapshot(self.database, self.snapshot, max_bytes=1)
        self.assertFalse(self.snapshot.exists())
        create_snapshot(self.database, self.snapshot)
        with self.assertRaises(SnapshotError):
            verify_snapshot(self.snapshot, max_bytes=1)

    def test_locked_database_obeys_deadline_and_cleans_output(self):
        with closing(sqlite3.connect(self.database)) as lock:
            lock.execute("BEGIN EXCLUSIVE")
            start = time.monotonic()
            with self.assertRaises((SnapshotError, sqlite3.Error)):
                create_snapshot(self.database, self.snapshot, timeout=0.2)
            self.assertLess(time.monotonic() - start, 3)
            lock.rollback()
        self.assertFalse(self.snapshot.exists())

    def test_restore_failure_removes_only_new_output(self):
        create_snapshot(self.database, self.snapshot)
        with patch(
            "sqlite_snapshot_kit.core._inspect",
            side_effect=[
                self.manifest()["database"],
                VerificationError("injected restore validation failure"),
            ],
        ):
            with self.assertRaises(VerificationError):
                restore_snapshot(self.snapshot, self.restored)
        self.assertFalse(self.restored.exists())
        self.assertTrue(verify_snapshot(self.snapshot)["ok"])

    def test_private_source_path_and_rows_are_not_in_manifest(self):
        create_snapshot(self.database, self.snapshot)
        text = (self.snapshot / core.MANIFEST).read_text(encoding="utf-8")
        self.assertNotIn(str(self.database), text)
        self.assertNotIn('"first"', text)
        self.assertNotIn('"second"', text)

    def test_source_disappearing_after_verification_cleans_new_restore_file(self):
        create_snapshot(self.database, self.snapshot)
        original_verify = core._verify

        def disappear(*args):
            manifest = original_verify(*args)
            (self.snapshot / core.PAYLOAD).unlink()
            return manifest

        with patch("sqlite_snapshot_kit.core._verify", side_effect=disappear):
            with self.assertRaises(OSError):
                restore_snapshot(self.snapshot, self.restored)
        self.assertFalse(self.restored.exists())

    def test_oversized_schema_statement_is_bounded_and_cleaned(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "CREATE TABLE large_default(id INTEGER, value TEXT DEFAULT '"
                + "x" * 1_048_576
                + "')"
            )
            connection.commit()
        with self.assertRaises(SnapshotError):
            create_snapshot(self.database, self.snapshot)
        self.assertFalse(self.snapshot.exists())

    def test_invalid_options(self):
        for timeout in [0, -1, True, float("nan"), float("inf"), 100_000]:
            with self.assertRaises(SnapshotError):
                create_snapshot(self.database, self.snapshot, timeout=timeout)
        for minimum in [{"": 1}, {"notes": -1}, {"notes": True}, []]:
            with self.assertRaises(SnapshotError):
                create_snapshot(self.database, self.snapshot, minimum_rows=minimum)

    def test_cli_core_flow_and_exit_codes(self):
        def cli(*args):
            return subprocess.run(
                [sys.executable, "-m", "sqlite_snapshot_kit", *map(str, args)],
                capture_output=True,
                text=True,
            )

        result = cli("create", self.database, self.snapshot, "--min-rows", "notes=2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["rows"], 2)
        self.assertEqual(cli("verify", self.snapshot).returncode, 0)
        self.assertEqual(cli("restore", self.snapshot, self.restored).returncode, 0)
        self.assertEqual(cli("restore", self.snapshot, self.restored).returncode, 2)
        self.assertEqual(cli("verify", self.snapshot, "--min-rows", "notes=3").returncode, 1)
        self.assertEqual(
            cli("create", self.database, self.root / "bad", "--timeout", "NaN").returncode, 2
        )


if __name__ == "__main__":
    unittest.main()
