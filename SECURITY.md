# Security

Only the current 0.1.x series is considered for fixes. This is early software.

Report a vulnerability through [GitHub private vulnerability reporting](https://github.com/KanadeK/sqlite-snapshot-kit/security/advisories/new)
if it is enabled. If that channel is unavailable, open a minimal issue requesting a private
contact without including exploit details or sensitive material. Do not post credentials,
real user captures, databases or private paths. The maintainer cannot guarantee a response time.

Use a maintained Python distribution with its security updates. The standard library and
its native libraries remain part of the trust boundary. Input limits reduce accidental
resource use; this program is not a sandbox for hostile native-parser exploits.
See README.md and docs/DESIGN.md for project-specific limitations.
