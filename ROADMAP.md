# Symphony Implementation Roadmap

Implementation plan for the Symphony service defined in [SPEC.md](SPEC.md), built on the stack in
[TECH.md](TECH.md) (Python 3.13; pytest; Black/Ruff/mypy; `dataclasses`/`pydantic`).

## Approach

Build bottom-up to **Core Conformance** (SPEC §18.1) in dependency order, so each milestone is
independently testable against the spec's test matrix (SPEC §17). Extensions (SPEC §18.2 and the
appendices) layer on top once the core is in place.

Decomposed into PR-sized increments. Each PR is one cohesive unit, ships with its own tests, and
leaves the repo green (CI + lint + type-check passing). Ordering is the dependency chain — each PR
depends only on earlier ones.

**Sizing legend:** `S` ≈ <150 lines + tests · `M` ≈ 150–400 lines + tests.

### Open decisions

- **Build order:** this plan assumes bottom-up / core-first. A vertical-slice-first alternative
  (thin end-to-end pipeline, then per-layer hardening) is possible but reshuffles the PR set.
- **State persistence:** SPEC §14.3 makes scheduler state intentionally in-memory, so **M0–M7 do
  not use Postgres/SQLAlchemy/Alembic/Redis**. That stack belongs to the optional persistence
  extension (E3). TECH.md lists it because the extension is anticipated, not because core needs it.
- **Extension scope:** which of E1–E5 are in scope is not yet decided; they are listed but not yet
  decomposed into PRs.

## Milestones (Core Conformance)

| # | Milestone | Delivers | SPEC | Exit criteria |
|---|-----------|----------|------|---------------|
| M0 | Scaffolding & domain model | tooling, package layout, CI, typed models | §4.1 | CI green, mypy clean |
| M1 | Workflow loader + config | parse, typed config, validation, reload, prompt rendering | §5, §6, §12 | §17.1 passes |
| M2 | Workspace manager + safety | sanitized workspaces, hooks, safety invariants | §9 | §17.2 passes |
| M3 | Linear tracker client | candidate/state/terminal fetch, normalization, errors | §11 | §17.3 passes |
| M4 | Claude CLI agent runner | per-turn headless subprocess, stream-json, continuation | §10 | §17.5 passes |
| M5 | Orchestrator core | in-memory state, poll tick, concurrency, retry, reconciliation | §7, §8, §16 | §17.4 passes |
| M6 | Observability + CLI host | structured logs, snapshot, CLI entrypoint | §13, §17.7 | §17.6, §17.7 pass |
| M7 | Wire-up + E2E | service startup/loop, real-integration profile | §16.1, §17.8 | §18.1 complete |

## Status

_Orientation snapshot. The authoritative state is always `git log origin/main --oneline` and
`gh pr list` — markers below can lag a merge._

- **M0 — complete** (merged): repo tooling/skeleton + `exceptions.py`; `Issue`/`BlockerRef` +
  normalization; remaining domain models + typed `ServiceConfig`. Also `docs/data-model.html`.
- **M1 — in progress** (§5, §6, §12):
  - ✅ PR 4 — WORKFLOW.md loader (`workflow_loader.py`) — merged
  - ✅ PR 5 — Config layer (`config_resolver.py`) — merged
  - ✅ PR 6 — Dispatch preflight (`preflight.py`) — merged
  - 🔄 PR 7 — Strict prompt rendering (`prompt_renderer.py`, python-liquid) — open PR, in review
  - ⬜ PR 8 — Dynamic reload/watch (§6.2) — **next**; completes M1
- **M2–M7 — not started.**

Modules merged on `main`: `exceptions`, `models`, `normalization`, `config`, `config_resolver`,
`preflight`, `workflow_loader`. Build the remaining PRs below in order, one PR each, leaving the
repo green (see `CLAUDE.md`).

## PR Breakdown

### M0 — Scaffolding & domain model

1. **Repo tooling & skeleton** (S) — `pyproject.toml` (Py 3.13), Ruff/Black/mypy/pytest config,
   `src/packages/symphony/` skeleton, GitHub Actions CI, `exceptions.py` hierarchy. No logic.
   Exit: CI green, lint/type/test all run.
2. **Issue model + normalization** (S) — `Issue` + blocker refs, identifier/state normalization,
   workspace-key sanitization (§4.2). Tests: normalization rules.
3. **Remaining domain models** (S) — `WorkflowDefinition`, `ServiceConfig` view, `Workspace`,
   `RunAttempt`, `LiveSession`, `RetryEntry`, `OrchestratorState` as typed containers.
   Tests: construction/defaults.

### M1 — Workflow loader + config (§5, §6, §12)

4. **WORKFLOW.md loader** (S) — front-matter/body split, YAML parse, error classes
   (`missing_workflow_file`, `workflow_parse_error`, `…_not_a_map`). Tests: §17.1 (loader).
5. **Config layer** (M) — typed getters, defaults, `$VAR`/`~` resolution, path normalization,
   per-state concurrency normalization. Tests: §17.1 (config).
6. **Dispatch preflight validation** (S) — startup + per-tick checks (§6.3). Tests.
7. **Strict prompt rendering** (M) — Liquid engine, `issue`+`attempt` vars, strict
   unknown-var/filter failure, empty-prompt fallback (§5.4, §12). Tests.
8. **Dynamic reload/watch** (M) — file watcher, re-read/re-apply, last-known-good on invalid,
   defensive re-validate (§6.2). Tests.

### M2 — Workspace manager + safety (§9)

9. **Workspace create/reuse + safety invariants** (M) — sanitized path under root, `created_now`
   gating, cwd/containment/sanitization invariants (§9.1–9.2, 9.5). Tests: §17.2 (paths/safety).
10. **Workspace hooks** (M) — 4 hooks, `timeout_ms`, failure semantics (§9.4). Tests.

### M3 — Linear tracker client (§11)

11. **GraphQL transport + error mapping** (M) — HTTP client, auth header, error categories,
    30s timeout (§11.2, §11.4). Tests (mocked).
12. **Issue normalization** (S) — labels/blockers (`blocks`)/priority/timestamps → `Issue`
    (§11.3). Tests.
13. **Three operations + pagination** (M) — candidate fetch (paginated, `slugId`),
    `fetch_issues_by_states`, `fetch_issue_states_by_ids` (`[ID!]`) (§11.1). Tests: §17.3.

### M4 — Claude CLI agent runner (§10)

14. **Flag builder + launch** (M) — argv from `claude` config, `bash -lc`, cwd=workspace,
    prompt via stdin (§10.1). Tests (fake binary).
15. **stream-json parser → normalized events** (M) — line parser, event mapping, malformed
    handling (§10.3–10.4). Tests with fixtures.
16. **Turn lifecycle + outcome + timeouts** (M) — init capture/`read_timeout`, `turn_timeout`,
    outcome from `result`+exit code, error mapping, token/cost extraction (§10.2, §10.6).
    Tests: fake `claude` script.
17. **Continuation (`--resume`) + Agent Runner contract** (M) — per-turn loop, session_id reuse
    (§10.7, §16.5). Tests.

### M5 — Orchestrator core (§7, §8, §16)

18. **State + claim/dispatch primitives** (M) — state ops, `dispatch_issue`, claim/running
    bookkeeping (§16.4). Tests.
19. **Candidate selection + sorting + concurrency** (M) — eligibility (labels/assignee/Todo-blocker),
    sort order, global+per-state slots (§8.2–8.3). Tests.
20. **Retry & backoff** (M) — continuation 1s, exponential failure backoff w/ cap, timer handling,
    slot-exhaustion requeue (§8.4, §16.6). Tests.
21. **Reconciliation** (M) — stall detection + tracker state refresh, terminal cleanup /
    non-active stop (§8.5). Tests: §17.4.
22. **Poll tick + startup cleanup + accounting** (M) — full tick sequence, startup terminal sweep,
    token/runtime aggregation (§8.1, §8.6, §13.5, §16.1–16.3). Tests.

### M6 — Observability + CLI host (§13, §17.7)

23. **Structured logging** (S) — context fields (issue_id/identifier/session_id), sink resilience
    (§13.1–13.2). Tests: §17.6.
24. **Runtime snapshot interface** (S) — snapshot shape, timeout/unavailable modes (§13.3). Tests.
25. **CLI entrypoint + host lifecycle** (M) — positional path, cwd default, startup failure,
    exit codes (§17.7). Tests.

### M7 — Wire-up + E2E

26. **Service startup & event-loop wiring** (M) — compose components, graceful shutdown (§16.1).
    Integration tests with fakes.
27. **Real Integration Profile** (S, CI-gated) — gated Linear smoke test, host-OS hook/path
    verification (§17.8, §18.3).

## Extensions (post-core, not yet scoped into PRs)

- **E1 — HTTP server + dashboard + JSON API** (§13.7): FastAPI; `/api/v1/state`, `/<id>`,
  `/refresh`.
- **E2 — Linear GraphQL MCP tool** (§10.5): MCP server registered via `--mcp-config`, credential
  held host-side.
- **E3 — Persistence** (§18.2 TODO): Postgres/SQLAlchemy/Alembic/Redis for durable retry queue +
  session metadata across restarts. **This is the only place the TECH.md DB stack is used.**
- **E4 — SSH worker** (Appendix A).
- **E5 — Humanized event summaries** (§13.6).
