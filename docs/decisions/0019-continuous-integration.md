# 0019 — Continuous integration: offline checks and repository policy

**Status:** Accepted (2026-11-20), between Milestones 5 and 6. Tooling and repository policy, not milestone functionality.
**Design refs:** §10.8 (freeze discipline), §11.8 (provenance), §15 Rules 22, 25, 26; CLAUDE.md (frozen design, run records, directories); decisions 0005 (toolchain), 0018 (run records)

## What CI enforces

One workflow, `.github/workflows/ci.yml`, on `push` and `pull_request`, with two jobs that run in parallel:

| Job | Enforces |
|---|---|
| `test` | **Clean-checkout reproducibility.** A fresh clone: `uv lock --check`, then the documented bootstrap `make sync`, then `make test` and `make lint` (which already includes both `ruff check` and `ruff format --check`). |
| `repository-policy` | `scripts/check_repo_policy.py`: the frozen design, tracked-artifact hygiene, and decision numbering. |

**Workflow hardening:**
- `permissions: contents: read` at the top level, with no job overriding it.
- Every `actions/checkout` sets `persist-credentials: false`.
- Every external action is pinned to a full commit SHA, with its release as a comment: `actions/checkout` v7.0.1, `astral-sh/setup-uv` v10.4.0. Both were checked to be commit SHAs (lightweight tags), and every input used was checked against the release's `action.yml`.
- Event values reach the shell only through `env:`, never interpolated into a `run:` script.

## The `test` job

- **The lock is checked before installing.** `make sync` stays plain `uv sync`, because locally it should be able to re-resolve while you deliberately change dependencies. CI claims more than that: this checkout reproduces from the committed lock. So it runs `uv lock --check` first, and a `pyproject.toml` change without a regenerated `uv.lock` fails there instead of being silently re-resolved. Verified: adding a dependency to `pyproject.toml` alone makes `uv lock --check` exit 1.
- **The documented bootstrap, not a private copy of it.** The job runs `make sync`; it doesn't reimplement it. A clean clone that needs undocumented local state is a repository bug, to be fixed in the bootstrap.
- **Python.** `UV_PYTHON_PREFERENCE=only-managed` makes uv provision its own CPython for the `.python-version` pin (`3.12`), never the runner's incidental interpreter. So there is no `actions/setup-python`: one fewer action to trust and pin.
  - uv itself is pinned (`version: "0.13.9"`, matching local development), which fixes which 3.12 build it provisions.
  - The job prints the interpreter and asserts it is 3.12.
  - Its cache is off, so a fresh clone proves the checks from Git alone.
- **Verified locally:** a clean clone of the repository, with no `.safeops/`, `reference/` or `.venv`, ran the job's steps under `only-managed`. The lock check passed, `make sync` built a venv on uv's managed CPython 3.12.11 (not Homebrew's or the system's), 810 tests passed and lint was clean.

## The policy checker (`scripts/check_repo_policy.py`)

It's stdlib-only and runnable locally: `python scripts/check_repo_policy.py --base <rev> --head <rev>`. It is deterministic and tested (`tests/test_repo_policy.py`, including a mutation check of every key property). Every check is **fail-closed**: a missing input, a failed Git command, an unknown revision, missing history or an unexpected exception is a failure with a diagnostic, never a pass.

1. **Frozen design.** No commit in `base..head` may modify `docs/safeops-design.md` unless *that commit's own message* contains `[allow-design-change]`.
   - The check walks every commit that touches the file (`git rev-list --full-history`) and checks each one's own message. A marker on another commit in the same push authorizes nothing.
   - The failure names the commit and says how to authorize an intentional change. It's an auditable exception, not a bypass.
   - **The range is validated in one place.** An empty base or head, the all-zero SHA (a new branch), a malformed or unknown revision, or a Git failure fails with "the design-freeze policy could not establish a valid commit range". It never reads as "nothing changed".
   - A push to a brand-new branch therefore fails this check, by design. A push to an existing branch supplies `before`/`after`, and a pull request supplies its base and head SHAs.
   - The policy job checks out with `fetch-depth: 0`, so the base is always reachable. A shallow clone would fail closed, and that would be a checkout error posing as a policy failure.
   - A merge commit that differs from its parents in the design file also needs the marker. That errs toward failing.
2. **Tracked-artifact hygiene** checks the whole tracked tree (`git ls-files`), not just the push, so a forbidden file committed months ago still fails today. These must never be tracked:
   - anything under `data/runs/`, `.safeops/` or `reference/`, anchored at the repository root;
   - `.env` and `.env.*`, and any file named `kubeconfig` or `*.kubeconfig`, at any depth.

   **This is about Git, not the working tree:** it's a backstop behind `.gitignore`, against `git add -f`, renamed local state and leaked credentials. Matching uses normalized path rules, never substrings, so `docs/decisions/reference-review.md` passes while `reference/foo.md` fails. Both cases are tests. There is **no `.env` template exception**. A real need for one would arrive as a deliberate policy change, with its exact filename, a rationale and a test.
3. **Decision numbering** checks the whole tracked tree.
   - Files are `NNNN-lowercase-words.md`: no duplicate numbers, and no gaps from `0001` to the highest.
   - The only non-numbered file allowed is **`reference-review.md`**, a named exception (an exact filename), not an open pattern, so `random-notes.md` fails.
   - An obsolete decision is marked superseded, never deleted, so there are no gaps.
   - Today, `0001` to `0019` are contiguous.

## Why

- **Reproducibility is part of the experimental claim.** Another person must be able to clone the repository and get the same offline checks without hidden local state. A reproducibility check has to verify the committed lock, not quietly produce a new dependency solution.
- **The design must not drift during the pilot** (§10.8). Until now that rule depended on someone remembering it.
- **Generated experiment data, local state and credential-adjacent files must never enter source control.** Run records are immutable observations, and kubeconfigs are credentials.
- **The decision log needs stable, auditable numbering.**
- **Policy checks fail closed.** A verifier that approves when it can't inspect its evidence is unacceptable in this project.
- **CI follows the same least privilege as the runtime system,** and its dependencies don't change underneath an experimental repository.

## Deliberately not added

- **Live cluster tests in CI** (kind, minikube, `make cluster-test`, `safeops steady-state`). They're slow and sensitive to infrastructure, and noisy red builds would teach the operator to ignore CI. Live validation stays a deliberate milestone-boundary action. Revisit only if a later milestone or the pilot gives a concrete reason.
- **A Dockerfile for the harness.** The components that need containers are containerized already, and the harness deliberately runs on the host against the local cluster.
- **Copilot or generic automated review.** This project gets value from design-aware adversarial review, with the frozen design, the decisions and the invariants in context. Generic review would mostly add noise.
- **Dependabot or any automated update bot,** for dependencies or for the actions' SHAs. Dependencies are part of recorded provenance, and a mid-campaign change splits runs meant to be comparable. Updates stay deliberate and happen at controlled milestone boundaries. That's not a claim that dependencies are frozen forever.
- **Branch protection or a mandatory PR workflow.** This is a solo workflow with review before commits, and there's no demonstrated need.
- **A coverage threshold.** A line-coverage percentage isn't a SafeOps acceptance criterion; the project uses targeted invariant tests, negative tests and mutation checks for safety- and experiment-critical behavior. That isn't a claim that mutation checks subsume coverage: they measure different things.
- Release, deployment or artifact-publishing automation.

## Carried to Milestone 12: campaign health, not CI (`safeops status`)

During a long Phase A campaign, the operator should be able to notice degradation before the experiment ends. Milestone 12 adds a lightweight `safeops status` that reads the persisted run records and answers one question: is this campaign still producing trustworthy data?

Its first version reports:
- the total number of records;
- kept and discarded;
- discard counts and rates by cause;
- counts by fault;
- the number of records under `rejected/`;
- the cohort G count, where it applies.

No dashboard. Not implemented now.
