# Security Policy

## Supported versions

Symphony-Claude is pre-1.0 and under active development. Security fixes are
applied to the `main` branch. Until a stable release is tagged, only `main` is
supported.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, report them privately so we can address them before disclosure:

- Preferred: use GitHub's [private vulnerability reporting](https://github.com/skwijeratne/symphony-claude/security/advisories/new)
  ("Report a vulnerability" under the repository's **Security** tab).
- Alternatively, email **skwijeratne@gmail.com** with the details.

Please include:

- A description of the vulnerability and its impact.
- Steps to reproduce, or a proof of concept.
- Affected version/commit and any relevant configuration.

You can expect an initial acknowledgement within a few days. We will keep you
informed of progress toward a fix and coordinate disclosure timing with you.

## Scope notes

Symphony orchestrates a coding agent (the Claude Code CLI) that executes with
real filesystem and tool access inside per-issue workspaces. When evaluating or
reporting issues, pay particular attention to:

- Workspace path handling and the containment invariants (SPEC §9.5).
- Handling of tracker credentials and `$`-resolved configuration values
  (SPEC §6.1).
- The agent harness hardening surface — the MCP servers, extra directories,
  filesystem paths, and network destinations exposed to the agent (SPEC §10.5).
