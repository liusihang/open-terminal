# Handoff - Open Terminal OfficeCLI Runtime Path Investigation

- Date: 2026-04-13
- Workspace: /Users/liusihang/open-terminal
- Base Branch/Commit: main @ 83eb8218f26ffb96ed2c3295a409434a8576936d
- Goal: Determine why Open Terminal can directly operate workspace files and implement the minimal clean path to make OfficeCLI operate on those same files through Open Terminal runtime.

## Checkpoints

1. Checkpoint: Confirm Open Terminal file access model
- Action: Read `open_terminal/main.py` and `open_terminal/utils/fs.py`.
- Evidence:
  - `/execute` runs commands directly in Open Terminal runtime (`create_runner` with local cwd/user context).
  - `/files/*` operations use local `aiofiles/os` via `UserFS`.
  - `UserFS.resolve_path` maps relative paths against user home and rewrites `/home/user` in multi-user mode.
- Result: Open Terminal can directly operate workspace files because command execution and file I/O happen in the same runtime and filesystem namespace.

2. Checkpoint: Confirm OfficeCLI integration gap source
- Action: Read OpenWebUI handoff context and inspect live OfficeCLI OpenAPI.
- Evidence:
  - External OpenAPI tool execution forwards parameters only; no terminal file bridge.
  - Existing OfficeCLI service (`mcpo`) runs in a different runtime from terminal workspace.
  - Live `/officecli` schema expects command fields like `command`, `file`, `path`, `props`, etc.
- Result: `File not found` is a runtime filesystem boundary issue, not parameter parsing issue.

3. Checkpoint: Validate OfficeCLI CLI mapping feasibility
- Action: SSH to aiserver and inspect `officecli --help` and subcommand helps.
- Evidence:
  - CLI supports positional `file/path/...` args and options (`--prop`, `--commands`, `--force`, etc.).
  - `--json` output is available for machine-readable responses.
- Result: A minimal `/officecli` endpoint in Open Terminal can map request fields to local CLI invocation and keep file paths in the same runtime.

4. Checkpoint: Implement minimal runtime-path fix in open-terminal
- Action: Add native `/officecli` endpoint and argument mapping in `open_terminal/main.py`.
- Evidence:
  - Added `OfficeCliRequest` model matching OfficeCLI OpenAPI field set.
  - Added `build_officecli_args()` to map API payload to `officecli` CLI args with `UserFS.resolve_path()` for `file`.
  - Added `run_officecli` endpoint to execute `officecli` in Open Terminal runtime and parse `--json` output.
  - Added timeout and error handling (`400` invalid input/command failure, `504` timeout).
- Result: OfficeCLI can now run against the same workspace filesystem that terminal commands use, without external tool-server filesystem mismatch.

5. Checkpoint: Verify with tests (TDD flow)
- Action:
  - Added tests first in `tests/test_officecli_endpoint.py`.
  - Ran tests before implementation to confirm failure.
  - Implemented minimal code.
  - Re-ran targeted and full test suites.
- Evidence:
  - Initial run: `uv run pytest tests/test_officecli_endpoint.py -q` -> failed during import (expected, symbols missing).
  - Final targeted run: `uv run pytest tests/test_officecli_endpoint.py -q` -> `19 passed`.
  - Full run: `uv run pytest -q` -> `19 passed`.
- Result: Behavior is covered for argument mapping, required-field validation, and endpoint execution/JSON parsing flow.

## Decision

- Root cause: runtime filesystem boundary between external OfficeCLI server and terminal workspace.
- Minimal viable path:
  1. Deployment-only path (no code): run `officecli` directly through Open Terminal `/execute` in the same runtime.
  2. Implemented path (cleaner integration): use new Open Terminal `/officecli` endpoint for structured OfficeCLI calls in the same runtime.
- Chosen implementation: option 2 in this repo to provide a direct, structured, root-cause-aligned interface while keeping changes minimal.
