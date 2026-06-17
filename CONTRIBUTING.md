# Contributing to Symphony-Claude

Thanks for your interest in contributing! This document covers how to set up a
development environment, the quality gate every change must pass, and the
conventions this project follows.

By participating you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- Read [`SPEC.md`](SPEC.md) — the normative specification this service implements
  — and [`TECH.md`](TECH.md) for the stack and coding standards.
- [`ROADMAP.md`](ROADMAP.md) tracks milestones and the planned PR breakdown.
- For anything beyond a small fix, please open an issue first so we can agree on
  the approach before you invest time.

## Development setup

The project targets **Python 3.13**. [`uv`](https://docs.astral.sh/uv/) is the
recommended toolchain:

```bash
# Install dependencies (runtime + dev tooling) into a local venv
uv sync --extra dev

# Run the test suite
uv run pytest
```

Plain `pip` works too:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## The quality gate

CI (`.github/workflows/ci.yml`) runs on Python 3.13 and **must be green** before a
PR can merge. Run the same checks locally before pushing:

```bash
uv run ruff check .       # lint
uv run black --check .    # format check  (uv run black . to apply)
uv run mypy               # type check
uv run pytest             # tests
```

A change is "done" only when all four pass.

## Coding standards

These mirror [`TECH.md`](TECH.md); the linters enforce most of them:

- **Formatting:** Black, line length 88. **Linting:** Ruff. **Types:** mypy —
  type hints on every function signature.
- **Docstrings:** Google style. Cite the relevant `SPEC.md` section numbers in
  docstrings and in the PR body.
- **Imports:** absolute only (no relative imports), ordered stdlib → third-party
  → local, no wildcard imports.
- Prefer `dataclasses`/`pydantic` over raw dicts, `pathlib.Path` over `os.path`,
  and `Enum` for fixed value sets. Use the `logging` module, never `print`, for
  application output.
- Custom exceptions live in `src/packages/symphony/exceptions.py`.
- Tests live in `tests/` mirroring `src/`, prefixed `test_`. Mock external
  services — tests must not hit real APIs or the network.

## Pull requests

- Branch off `main` (`git checkout main && git pull --ff-only` first). Use
  descriptive branch names like `feat/<slug>`, `fix/<slug>`, or `docs/<slug>`.
- Keep each PR scoped to one logical change; note any follow-up work a later PR
  should own.
- Write a clear PR description: what changed, why, and any design decisions or
  trade-offs. Reference the issue it closes and the `SPEC.md` sections involved.
- Ensure CI is green (`gh pr checks <n>`). A maintainer reviews and merges.

## Reporting bugs and requesting features

Use the [issue templates](.github/ISSUE_TEMPLATE/). For security issues, please
follow [`SECURITY.md`](SECURITY.md) instead of opening a public issue.

## License

By contributing, you agree that your contributions will be licensed under the
[Apache License 2.0](LICENSE) that covers this project.
