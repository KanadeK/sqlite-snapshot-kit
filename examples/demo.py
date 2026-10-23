"""Exercise a live WAL backup and query the restored database, using synthetic data only."""

import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database, snapshot, restored = (
            root / "app.sqlite3",
            root / "snapshot",
            root / "restored.sqlite3",
        )
        with closing(sqlite3.connect(database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
            writer.execute("INSERT INTO notes(body) VALUES (?)", ("synthetic WAL row",))
            writer.commit()
            assert Path(str(database) + "-wal").exists()
            for command in [
                ["create", str(database), str(snapshot), "--min-rows", "notes=1"],
                ["verify", str(snapshot)],
                ["restore", str(snapshot), str(restored)],
            ]:
                subprocess.run([sys.executable, "-m", "sqlite_snapshot_kit", *command], check=True)
        with closing(sqlite3.connect(restored)) as connection:
            assert connection.execute("SELECT body FROM notes WHERE id=1").fetchone() == (
                "synthetic WAL row",
            )
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        print(
            "PASS: committed live-WAL data survived backup, verification and a real restore query."
        )


if __name__ == "__main__":
    main()
