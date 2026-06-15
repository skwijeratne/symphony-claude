# Symphony-Claude

[![CI](https://github.com/skwijeratne/symphony-claude/actions/workflows/ci.yml/badge.svg)](https://github.com/skwijeratne/symphony-claude/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)

A long-running Python service that orchestrates the **Claude Code CLI** (headless
mode) to deliver tracker-driven work. Symphony continuously reads issues from an
issue tracker (Linear), creates an isolated workspace for each one, and runs a
coding-agent session against it — turning issue execution into a repeatable
daemon instead of manual scripting.

It is a bottom-up implementation of the **Symphony Service Specification**
([`SPEC.md`](SPEC.md)), built toward Core Conformance (SPEC §18.1).

## How it works

```
WORKFLOW.md ──► poll tracker ──► select eligible issues ──► per-issue workspace
                    ▲                                              │
                    │                                              ▼
              reconcile / retry  ◄──── worker: Claude Code (headless, stream-json)
```

On each poll tick the orchestrator:

1. **Reconciles** running work against the tracker (stall detection + state
   refresh), stopping runs whose issues went terminal or inactive.
2. **Validates** dispatch config (preflight) and **fetches** candidate issues.
3. **Dispatches** eligible issues, in priority order, up to the concurrency
   limit — each on a worker that creates the workspace, runs lifecycle hooks, and
   drives a `claude --print --output-format stream-json` session turn by turn.
4. **Accounts** for tokens, cost, and runtime, and **schedules retries**
   (continuation after a clean exit, exponential backoff after a failure).

Everything is driven from a single version-controlled `WORKFLOW.md` that is
hot-reloaded without a restart. Observability is structured `key=value` logging
plus an optional runtime snapshot for dashboards/monitoring.

## Status

**Alpha / pre-1.0**, under active development. The core milestones (workflow
loading, config + dynamic reload, workspace management, the Linear tracker
client, the Claude Code agent runner, the orchestrator, observability, the CLI,
and the composed service event loop) are implemented and covered by tests. The
remaining work toward full Core Conformance is the gated real-integration
profile (SPEC §17.8). See [`ROADMAP.md`](ROADMAP.md) for the detailed status.

## Requirements

- **Python 3.13+**
- The [**Claude Code CLI**](https://code.claude.com/docs/en/headless) available
  on `PATH` (configurable via `claude.command`)
- A **Linear** API key and project for the tracker integration

## Installation

Using [uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv sync                      # runtime deps
uv sync --extra dev          # + dev tooling (linters, mypy, pytest)
```

Or with pip:

```bash
pip install -e .             # runtime
pip install -e ".[dev]"      # + dev tooling
```

This installs the `symphony` console script.

## Quickstart

Create a `WORKFLOW.md` in your working directory. It is YAML front matter (the
configuration) followed by the prompt template body:

```markdown
---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY      # $-resolved from the environment
  project_slug: my-team
polling:
  interval_ms: 30000
agent:
  max_concurrent_agents: 5
workspace:
  root: ./.symphony/workspaces
---
You are working on issue {{ issue.identifier }}: {{ issue.title }}.

Implement the change described in the issue, then stop.
```

Then run the service (the path is optional; it defaults to `./WORKFLOW.md`):

```bash
export LINEAR_API_KEY=lin_...
symphony                       # or: symphony path/to/WORKFLOW.md
```

`Ctrl-C` (or `SIGTERM`) triggers a graceful shutdown. The process exits `0` on a
normal start-and-shutdown and non-zero on a startup failure.

## Run with Docker

The provided [`Dockerfile`](Dockerfile) bundles the service together with git
and the Node-based Claude Code CLI, so the container can run an end-to-end loop.

```bash
docker build -t symphony-claude .

docker run --rm -it \
  -v "$PWD:/work" \
  -e LINEAR_API_KEY \
  -e ANTHROPIC_API_KEY \
  symphony-claude
```

Mount your `WORKFLOW.md` (and, if your hooks operate on it, your repo) at `/work`
— the default working directory — and pass `LINEAR_API_KEY` (for the tracker) and
`ANTHROPIC_API_KEY` (for the Claude Code CLI) as environment variables. Pass a
path argument to use a different workflow file:
`docker run ... symphony-claude configs/WORKFLOW.md`.

The image installs only `git`, Node.js, and the Claude Code CLI. If your hooks or
the agent need a language toolchain (e.g. a specific Python, Node, or package
manager to build the target repo), extend the image with a `FROM symphony-claude`
layer that adds it.

## Configuration

All configuration lives in `WORKFLOW.md` front matter and is resolved into a
typed config with defaults and `$VAR` indirection. The full schema — tracker,
polling, workspace, hooks, agent, and `claude` settings — is specified in
[`SPEC.md`](SPEC.md) §5.3 and §6, with a cheat sheet in §6.4.

For a fully annotated starting point — every field explained, with an opinionated
set of choices for a single-machine deployment — see
[`docs/WORKFLOW.sample.md`](docs/WORKFLOW.sample.md).

## Development

CI runs on Python 3.13 and must be green to merge. Run the same gate locally:

```bash
uv run ruff check .       # lint
uv run black --check .    # format check
uv run mypy               # type check
uv run pytest             # tests
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full workflow and coding
standards, and [`TECH.md`](TECH.md) for the stack.

## Project layout

```
src/packages/symphony/   # the `symphony` import package
tests/                   # pytest suite, mirroring src/
docs/                    # diagrams and explainers
SPEC.md                  # normative specification
TECH.md                  # stack + coding standards
ROADMAP.md               # milestones and PR breakdown
```

## Documentation

- [`SPEC.md`](SPEC.md) — the normative specification (the source of truth).
- [`TECH.md`](TECH.md) — stack and coding standards.
- [`ROADMAP.md`](ROADMAP.md) — milestones and status.
- [`docs/WORKFLOW.sample.md`](docs/WORKFLOW.sample.md) — an annotated example
  workflow file.
- `docs/` — HTML diagrams of the data model, retry/dispatch flow, and the
  service tick loop.

## Contributing

Contributions are welcome — please read [`CONTRIBUTING.md`](CONTRIBUTING.md) and
our [Code of Conduct](CODE_OF_CONDUCT.md) first. For security issues, follow
[`SECURITY.md`](SECURITY.md) rather than opening a public issue.

## License

This repository is licensed in two parts:

- **Implementation code** (everything under `src/` and `tests/`) — the
  [MIT License](LICENSE).
- **[`SPEC.md`](SPEC.md)** — adapted from OpenAI's Symphony specification and
  retained under the [Apache License 2.0](LICENSE-SPEC) (Copyright 2025 OpenAI).
  See [`NOTICE`](NOTICE) for attribution.

## Acknowledgements

Symphony-Claude is an independent implementation of
**[Symphony](https://github.com/openai/symphony)** by OpenAI. Its
[specification](https://github.com/openai/symphony/blob/main/SPEC.md) is the
basis for this project — OpenAI's README explicitly invites independent
implementations built from the spec, and this is one such implementation,
targeting the Claude Code CLI (headless) in place of the Codex app-server.
