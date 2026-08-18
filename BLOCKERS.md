# Environment-Exclusive Blockers

Record work here only when it cannot be implemented or conclusively verified from macOS.
Do not list ordinary defects, missing tests, design decisions, or optional platform coverage.

## Active blockers

### Native Windows verification of repaired Make targets

- **Why macOS cannot close it:** Simulating `OS=Windows_NT` verifies GNU Make expansion, but cannot
  execute Windows virtual-environment paths or validate Windows temporary-directory behavior.
- **Required environment:** A native Windows host with GNU Make, Python 3.12, and the project
  dependencies installed in `.venv`.
- **Exit criteria:** `make test`, `make lint`, and `make verify-warnings` exit successfully, and
  representative SQL target dry-runs plus a smoke command use `.venv/Scripts` and the native
  temporary directory without path errors.
- **Current evidence:** `make -n OS=Windows_NT ...` is useful simulated evidence only; it is not
  native verification.
