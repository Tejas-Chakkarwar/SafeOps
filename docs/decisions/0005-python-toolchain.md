# 0005 — Python toolchain: uv, Python 3.12, Typer, pytest, ruff

**Status:** Accepted (2026-11-18)
**Design refs:** §13 (stack); CLAUDE.md "Do not add a dependency"

## Decision

| Item | Choice |
|---|---|
| Project and environment manager | `uv` (standalone, 0.13.x) with `pyproject.toml` and a committed `uv.lock` |
| Build backend | `uv_build`, so no extra build tool is needed |
| Python | **3.12**, pinned two ways: `requires-python = ">=3.12,<3.13"` constrains resolution, and `.python-version` (`3.12`) makes uv's interpreter choice explicit instead of inheriting whatever is on PATH |
| Runtime dependency (Milestone 1) | `typer`, the CLI framework named in the §13 stack |
| Dev-only | `pytest` (tests, `make test`), `ruff` (lint and format check, `make lint`) |

## Notes

- **Python 3.12 vs "3.11+".** §13 and CLAUDE.md say "Python 3.11+". The 3.12 pin sits inside that range and is deliberately narrower, so every run uses the same interpreter minor version. Run provenance should record the exact interpreter version when the record schema is built (Milestone 5).
- **Dependencies arrive when first needed.** Pydantic, `kubernetes` and `prometheus-api-client` are in the §13 stack but are not used at Milestone 1, so they aren't declared yet. Pydantic happens to be installed already as a transitive dependency of Typer 0.29. Still declare it directly when SafeOps first imports it, and never rely on a transitive install. Each is added at the milestone that first needs it (Pydantic for the run record or action parsing, `kubernetes` at Milestone 2 or 3, `prometheus-api-client` with the metrics tool). Anything not in the §13 stack still needs its own decision entry.
- **`reference/` is excluded** from ruff (`extend-exclude`). pytest collects only `tests/` (`testpaths`), so third-party code is never linted or collected.
- **PATH on the development machine.** Anaconda's `uv` (0.8.x) and `/usr/local/bin/kubectl` (1.32) appear on PATH ahead of the intended binaries in older shells. `~/.zshrc` now puts `~/.local/bin` and `/opt/homebrew/bin` first. A shell started before that change resolves the wrong versions, so open a fresh shell.
