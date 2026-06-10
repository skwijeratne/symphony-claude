# CLAUDE.md

Working guidance for this repo. See also `AGENTS.md` (required reading), `SPEC.md` (the normative
specification), `TECH.md` (stack + coding standards), and `ROADMAP.md` (plan + current status).

## What this is

Symphony is a **Python 3.13** service that orchestrates the Claude Code CLI (headless mode) to
deliver tracker-driven work. It is being built **bottom-up** toward Core Conformance (SPEC §18.1).

## How work is delivered

- **One PR per `ROADMAP.md` "PR Breakdown" item**, in dependency order. Keep scope to that single
  item; explicitly note work a later PR owns.
- Branch off `main` (`git checkout main && git pull --ff-only` first); branches like
  `feat/<slug>` / `docs/<slug>`.
- Read the cited `SPEC.md` sections before coding and cite section numbers in docstrings and the PR
  body. Surface design decisions/judgment calls in the PR body.
- The maintainer reviews and **merges** each PR — do not merge yourself. After a merge, sync `main`
  and start the next item.
- End commit messages with the `Co-Authored-By:` trailer and PR bodies with the Claude Code
  generated-with line.

## Finding the current state

- `git log origin/main --oneline` — what is merged.
- `gh pr list` — what is open / in review.
- `ROADMAP.md` → "Status" — milestone-level snapshot (may lag a merge).

## CI is the gate

CI (`.github/workflows/ci.yml`) runs on **Python 3.13**: `ruff check .`, `black --check .`, `mypy`,
`pytest`. A PR is done only when CI is green (`gh pr checks <n>`).

- Layout: `src/packages/symphony/` (import package `symphony`); tests in `tests/` mirror it.
- Standards live in `TECH.md` (Black line length 88, Google docstrings, absolute imports only,
  `dataclasses`/`pydantic` over raw dicts, `Enum` for fixed value sets, custom exceptions in
  `exceptions.py`).
- The dev sandbox may have **no `pip`/network**, so the linters/tests cannot run locally — CI runs
  them. Pre-check what you can (`python -m py_compile`, line length, pure-Python logic), and verify
  any new dependency's API from its docs before adding it.
