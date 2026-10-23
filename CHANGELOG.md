# Changelog

## 0.1.0

- Create standalone snapshots through the SQLite Online Backup API, including committed WAL data.
- Write strict SHA-256/schema/count manifests and verify integrity, foreign keys and row minimums.
- Restore only into a new file, then check the actual restored database.
- Bound operations, reject ambiguous manifests/sidecars and include synthetic restore drills and CI.
- Require Python 3.12+ defensive configuration and reject virtual-table databases before query inspection.
