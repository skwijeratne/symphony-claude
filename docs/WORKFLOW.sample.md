---
# =============================================================================
# Sample WORKFLOW.md
# -----------------------------------------------------------------------------
# This is an annotated, opinionated example showing one reasonable set of
# choices for a single-machine deployment. Copy it to ./WORKFLOW.md, replace the
# placeholders (project_slug, the label, the repo URL), review the SAFETY block
# under `claude`, and run `symphony`.
#
# The front matter below is YAML (config); the Markdown after the closing `---`
# is the per-issue prompt template. Only four things are strictly REQUIRED:
# tracker.kind, tracker.api_key, tracker.project_slug, and a reachable
# `claude` command. Everything else has a default and can be omitted.
# Full schema: SPEC.md §5.3 and §6.4. The file hot-reloads — edit it while the
# service runs and changes apply to future ticks.
# =============================================================================

tracker:
  kind: linear                     # REQUIRED. Only `linear` is supported today.
  api_key: $LINEAR_API_KEY         # REQUIRED. `$VAR` is resolved from the env at
                                   # startup/reload — never hardcode a token.
                                   # Export LINEAR_API_KEY before launching.
  project_slug: your-team-slug     # REQUIRED for linear. The project to watch.

  # CHOICE: gate work behind an explicit label so the service only touches
  # issues you have opted in. Strongly recommended — without it, every issue in
  # an active state below becomes eligible. An issue must carry ALL listed
  # labels (case/space-insensitive) to be dispatched or continued.
  required_labels: [agent-ready]

  # CHOICE: pick up brand-new work (Todo) and resume work already underway
  # (In Progress). These are the defaults, shown here for clarity.
  active_states: [Todo, In Progress]

  # terminal_states defaults to [Closed, Cancelled, Canceled, Duplicate, Done].
  # An issue moving into one of these stops its run and cleans its workspace.
  # Uncomment to override:
  # terminal_states: [Done, Cancelled, Canceled, Duplicate]

polling:
  interval_ms: 30000               # Poll Linear every 30s. Lower = faster pickup
                                   # but more API calls. Default 30000.

workspace:
  # CHOICE: a visible, predictable location next to this file (relative paths
  # resolve against the WORKFLOW.md directory) instead of the system-temp
  # default. Add it to your .gitignore. One subdirectory is created per issue.
  root: ./.symphony/workspaces

hooks:
  # Hooks are PLAIN shell scripts (not Liquid-templated) run with the workspace
  # as cwd. Symphony does not inject issue env vars; the workspace directory
  # name is the sanitized issue identifier (use `basename "$PWD"`), and the
  # hooks inherit whatever environment you exported when launching `symphony`.

  # CHOICE: on a fresh workspace, clone the target repo into it and start a
  # branch for the issue. Export SYMPHONY_REPO_URL before launching. A non-zero
  # exit here ABORTS workspace creation (SPEC §9.4).
  after_create: |
    set -euo pipefail
    git clone "$SYMPHONY_REPO_URL" .
    git checkout -b "agent/$(basename "$PWD")"

  # CHOICE: install dependencies before each attempt. A non-zero exit ABORTS the
  # attempt. Adapt to your stack (npm ci / uv sync / poetry install / ...).
  before_run: |
    set -euo pipefail
    if [ -f package.json ]; then npm ci; fi
    if [ -f pyproject.toml ]; then uv sync --quiet || pip install -e .; fi

  # after_run and before_remove are OPTIONAL and best-effort (failures are
  # logged, not fatal). Example: capture a diff for debugging.
  # after_run: |
  #   git --no-pager diff > /tmp/symphony-$(basename "$PWD").diff || true

  timeout_ms: 120000               # CHOICE: 120s (default 60s) — clone + install
                                   # can exceed the default. Applies to all hooks.

agent:
  # CHOICE: cap parallelism to 3 on a single machine (default is 10). Each agent
  # is a full Claude Code session; bound this by cost and machine resources.
  max_concurrent_agents: 3

  max_turns: 20                    # Max turns per worker session. Default 20.
  # max_retry_backoff_ms: 300000   # Failure backoff cap (5m). Default shown.
  # max_concurrent_agents_by_state: { "in progress": 2 }  # optional per-state cap

claude:
  # command defaults to `claude`. Set it only if the CLI is elsewhere on PATH.
  # command: claude

  # CHOICE: `sonnet` is a balanced default for routine tasks. Use `opus` for
  # harder work, or a full model id. Omit to use the CLI's configured default.
  model: sonnet

  # ===========================================================================
  # SAFETY — review before pointing this at anything you care about (SPEC §10.5,
  # §9.5). Headless runs cannot prompt a human, so you either (a) whitelist tools
  # and auto-accept edits, as below, or (b) run fully unattended with
  # `permission_mode: bypassPermissions` ONLY inside a disposable/sandboxed
  # environment. This sample takes the safer (a) path.
  # ===========================================================================
  permission_mode: acceptEdits     # auto-accept file edits; other tools gated by
                                   # the allow/deny lists below.

  # CHOICE: least-privilege tool whitelist. Adapt to your stack. Patterns like
  # `Bash(git *)` scope shell access to specific commands.
  allowed_tools:
    - Read
    - Edit
    - Write
    - Grep
    - Glob
    - Bash(git *)
    - Bash(npm *)
    - Bash(pytest *)

  # CHOICE: explicitly deny destructive shell even if a broad pattern allows it.
  disallowed_tools:
    - Bash(rm *)
    - Bash(sudo *)

  # CHOICE: standing instructions appended to every session's system prompt.
  append_system_prompt: >-
    Always run the project's test suite and report the result before declaring
    the work done. Never force-push, delete branches, or touch files outside
    this workspace.

  # add_dirs: [../shared]          # widens the filesystem boundary — use sparingly
  # mcp_config: ./mcp.json         # register extra MCP tool servers
  # turn_timeout_ms: 3600000       # per-turn wall clock (default 1h)
  # read_timeout_ms: 5000          # wait for the first stream event (default 5s)
  # stall_timeout_ms: 300000       # no-event stall kill (default 5m; <=0 disables)
---
You are an autonomous software engineer. You have been assigned exactly one issue
from Linear and a fresh workspace containing the repository. Complete the issue
end to end, then stop.

## Issue {{ issue.identifier }} — {{ issue.title }}

- State: {{ issue.state }}
- Link: {{ issue.url }}
{% if issue.labels %}- Labels: {{ issue.labels | join: ", " }}{% endif %}

{{ issue.description }}

## What to do

1. Explore the repository in this workspace to understand the relevant code.
2. Implement the change described in the issue. Keep the change focused on this
   issue; note any out-of-scope work you notice rather than doing it.
3. Add or update tests that cover your change.
4. Run the project's test suite and make it pass.
5. Commit your work on the branch already checked out in this workspace, with a
   clear message that references {{ issue.identifier }}.

## Proof of work

Before you finish, summarize: what you changed and why, the test command you ran
and its result, and anything a reviewer should double-check.

{% if attempt %}
> This is continuation/retry attempt {{ attempt }}. Review what was already done
> in this workspace (git log, uncommitted changes) before continuing, and avoid
> redoing completed work.
{% endif %}
