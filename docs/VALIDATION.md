# Local validation — 2026-10-23

Version: 0.1.0. Requires Python 3.12+. This is the pre-publication local test record.

| Actual check | Result |
|---|---|
| Windows Python 3.13.16 unittest | 32 run, 29 passed, 3 conditional skips |
| Linux Python 3.12.15 minimum-version unittest | 32 passed, no skips |
| Linux Python 3.13.17 unittest | 32 passed, no skips |
| Fresh Windows wheel install, outside-checkout CLI demo, version, pip check, uninstall | Passed |
| Fresh Linux offline wheel install and outside-checkout CLI demo | Passed |
| Ruff 0.14.10 lint and format | Passed |
| sdist → wheel build and wheel/source comparison | Passed |
| Local author/committer history and co-author trailer audit | Passed |
| Credential/private-email/machine-path pattern scan of tracked files | No matches |

Windows skips are actual platform conditions: symlink creation privileges; this project's
POSIX-only filename case if present. Linux exercises those cases. The complete suites include
CLI integration and exceptional inputs, not only isolated unit functions. macOS has a CI job
configuration but has not been run locally.

No virtual tables, encryption, signatures, scheduling or PITR. Use a maintained vendor-patched SQLite runtime.

All runtime dependencies are from Python's standard library. No third-party source or assets
were bundled. The project is MIT; its license does not replace the separately supplied Python
runtime's licenses. The pattern scan and review do not prove absence of all possible security
problems. See README.md, SECURITY.md and DESIGN.md for the actual boundaries.

Current remote checks are available from [GitHub Actions](https://github.com/KanadeK/sqlite-snapshot-kit/actions/workflows/ci.yml).
The publication gate additionally verifies owner, commit attribution, exactly one contributor
and the Release tag. The historical local checks above alone do not prove those remote outcomes.
