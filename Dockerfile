# syntax=docker/dockerfile:1
#
# Symphony-Claude container image.
#
# Symphony is a Python service, but to do real work it drives the Node-based
# Claude Code CLI (headless) and the agent/hooks use git. This image therefore
# bundles all three: the Python service, a recent Node.js + the Claude Code CLI,
# and git — so a container can run an end-to-end orchestration loop.
#
# Build:
#   docker build -t symphony-claude .
#
# Run (mount your WORKFLOW.md + repo at /work, pass secrets as env):
#   docker run --rm -it \
#     -v "$PWD:/work" \
#     -e LINEAR_API_KEY \
#     -e ANTHROPIC_API_KEY \
#     symphony-claude
#
# `symphony` reads ./WORKFLOW.md from /work by default; pass a path argument to
# override (e.g. `docker run ... symphony-claude configs/WORKFLOW.md`).

FROM python:3.13-slim

LABEL org.opencontainers.image.title="Symphony-Claude" \
      org.opencontainers.image.description="Orchestrates the Claude Code CLI (headless) to deliver tracker-driven work." \
      org.opencontainers.image.source="https://github.com/skwijeratne/symphony-claude" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    NODE_MAJOR=22

# System dependencies:
#   git           — workspaces and the sample hooks clone/branch/commit
#   ca-certificates, curl — TLS + fetching the NodeSource setup script
#   Node.js + npm — runtime for the Claude Code CLI
# Then install the Claude Code CLI globally (provides the `claude` binary on PATH).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates gnupg \
 && curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code \
 && npm cache clean --force \
 && apt-get purge -y curl gnupg \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# Install the Symphony service. Copy only what the build needs (pyproject reads
# README.md and references LICENSE via license-files; the package is under src/).
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

# Run as a non-root user. /work is the mount point for WORKFLOW.md and, by
# default, the per-issue workspaces (workspace.root: ./.symphony/workspaces).
# NOTE: a bind-mounted /work carries the host's ownership; if the agent can't
# write there, run with `--user "$(id -u):$(id -g)"` or chown the mount.
RUN useradd --create-home --uid 1000 symphony \
 && mkdir -p /work \
 && chown symphony:symphony /work
USER symphony
WORKDIR /work

# Exec form so `symphony` is PID 1 and receives SIGTERM directly — the service
# installs a handler and shuts down gracefully (docker stop works as intended).
ENTRYPOINT ["symphony"]
