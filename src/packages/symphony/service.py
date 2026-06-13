"""Service composition and event loop (SPEC §16.1; ROADMAP M7).

:class:`SymphonyService` wires the deterministic orchestrator algorithms to a
concrete runtime: worker threads running
:func:`~symphony.agent_runner.run_agent_attempt`, ``threading.Timer`` retry and
tick timers, the :class:`~symphony.reload.WorkflowReloader` watch, the Linear
tracker client, and the runtime snapshot. The runtime model is a single
orchestrator thread consuming an event queue — every timer, worker thread, and
snapshot consumer communicates by enqueuing an event, so only the event loop
ever touches :class:`~symphony.models.OrchestratorState` and the single-writer
rule of SPEC §7 holds without locks around state.

Startup follows SPEC §16.1: the caller configures logging, then
:meth:`SymphonyService.serve` validates dispatch config (failing startup on a
:class:`~symphony.exceptions.DispatchPreflightError`), runs the startup
terminal-workspace sweep, schedules an immediate first tick, and enters the
event loop. :meth:`SymphonyService.stop` requests a graceful shutdown from any
thread: pending timers are cancelled, no new work is dispatched, and running
worker threads are given a bounded grace period to finish their current turn.

Dynamic reload (SPEC §6.2) is applied at the top of every tick via the config
source's ``poll()``: a changed file re-derives the polling cadence, concurrency
limits, dispatch policy, retry backoff cap, and tracker client, while in-flight
workers keep the config snapshot they started with.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from symphony.agent_runner import AttemptResult, run_agent_attempt
from symphony.config import ServiceConfig
from symphony.exceptions import (
    SnapshotTimeoutError,
    SnapshotUnavailableError,
    SymphonyError,
)
from symphony.linear_client import LinearClient
from symphony.linear_transport import LinearTransport
from symphony.models import Issue, OrchestratorState, RunningEntry
from symphony.orchestrator import (
    DispatchPolicy,
    RetryScheduler,
    dispatch_issue,
    on_agent_update,
    on_retry_timer,
    on_worker_exit,
    reconcile_running_issues,
    run_tick,
    startup_terminal_workspace_cleanup,
)
from symphony.preflight import check_dispatch_preflight, ensure_dispatchable
from symphony.reload import EffectiveConfig, WorkflowReloader
from symphony.snapshot import RuntimeSnapshot, build_snapshot
from symphony.stream_parser import AgentEvent
from symphony.structured_logging import log_fields

__all__ = [
    "TrackerClient",
    "TrackerFactory",
    "RunAttemptFn",
    "SymphonyService",
]

logger = logging.getLogger("symphony.service")


class TrackerClient(Protocol):
    """The tracker operations the service composes (SPEC §11.1).

    Satisfied by :class:`~symphony.linear_client.LinearClient` and by the fake
    trackers the integration tests inject.
    """

    def fetch_candidate_issues(self) -> list[Issue]: ...

    def fetch_issues_by_states(self, state_names: Sequence[str]) -> list[Issue]: ...

    def fetch_issue_states_by_ids(self, issue_ids: Sequence[str]) -> list[Issue]: ...


# Builds a tracker client for the current config; rebuilt on reload (SPEC §6.2).
TrackerFactory = Callable[[ServiceConfig], TrackerClient]

# Runs one worker attempt; signature-compatible with ``run_agent_attempt`` as the
# service calls it. Injectable so integration tests use fake workers.
RunAttemptFn = Callable[..., AttemptResult]


def _default_tracker_factory(config: ServiceConfig) -> TrackerClient:
    """Build the real ``LinearClient`` from tracker config (SPEC §11)."""
    transport = LinearTransport(config.tracker.endpoint, config.tracker.api_key or "")
    return LinearClient(
        transport,
        config.tracker.project_slug or "",
        config.tracker.active_states,
    )


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


# --- Event-loop messages ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Tick:
    """Run one poll-and-dispatch tick (SPEC §16.2)."""


@dataclass(frozen=True, slots=True)
class _RetryDue:
    """A retry timer fired for an issue (SPEC §16.6)."""

    issue_id: str


@dataclass(frozen=True, slots=True)
class _WorkerExit:
    """A worker thread finished its attempt (SPEC §16.6)."""

    issue_id: str
    result: AttemptResult


@dataclass(frozen=True, slots=True)
class _AgentUpdate:
    """A worker forwarded one agent ``stream-json`` event (SPEC §7.3)."""

    issue_id: str
    event: AgentEvent


@dataclass(frozen=True, slots=True)
class _SnapshotRequest:
    """A consumer thread wants a runtime snapshot (SPEC §13.3)."""

    reply: queue.SimpleQueue[RuntimeSnapshot]


@dataclass(frozen=True, slots=True)
class _Stop:
    """Graceful shutdown was requested."""


_Event = _Tick | _RetryDue | _WorkerExit | _AgentUpdate | _SnapshotRequest | _Stop


class SymphonyService:
    """The composed service: orchestrator algorithms bound to a thread runtime.

    Args:
        config_source: The live source of effective configuration — a
            :class:`~symphony.reload.WorkflowReloader` holding the last known
            good config and re-applying it on ``WORKFLOW.md`` changes. Its
            initial load has already succeeded (SPEC §16.1 startup is strict
            about that).
        tracker_factory: Builds the tracker client for a config; defaults to
            the real Linear client. Injectable for integration tests.
        run_attempt: The worker attempt function; defaults to
            :func:`~symphony.agent_runner.run_agent_attempt`. Injectable for
            integration tests with fake workers.
        shutdown_grace_s: How long :meth:`serve` waits for running worker
            threads to finish after a stop request before detaching from them.
        now: Clock seam for state timestamps.
    """

    def __init__(
        self,
        config_source: WorkflowReloader,
        *,
        tracker_factory: TrackerFactory = _default_tracker_factory,
        run_attempt: RunAttemptFn = run_agent_attempt,
        shutdown_grace_s: float = 5.0,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._config_source = config_source
        self._tracker_factory = tracker_factory
        self._run_attempt = run_attempt
        self._shutdown_grace_s = shutdown_grace_s
        self._now = now

        self._events: queue.Queue[_Event] = queue.Queue()
        self._tick_timer: threading.Timer | None = None
        self._serving = False

        effective = config_source.current
        self._state = OrchestratorState(
            poll_interval_ms=effective.config.polling.interval_ms,
            max_concurrent_agents=effective.config.agent.max_concurrent_agents,
        )
        self._apply_config(effective)

    # --- composition --------------------------------------------------------

    def _apply_config(self, effective: EffectiveConfig) -> None:
        """Adopt ``effective`` for all future decisions (SPEC §6.2).

        Rebuilds everything derived from config — dispatch policy, tracker
        client, retry backoff cap — and adjusts the live polling cadence and
        concurrency limit. In-flight workers keep the snapshot they started
        with (SPEC §6.2 does not require restarting them).
        """
        self._config = effective.config
        self._prompt_template = effective.prompt_template
        self._policy = DispatchPolicy.from_config(effective.config)
        self._tracker = self._tracker_factory(effective.config)
        self._scheduler = RetryScheduler(
            set_timer=self._set_retry_timer,
            cancel_timer=self._cancel_retry_timer,
            max_retry_backoff_ms=effective.config.agent.max_retry_backoff_ms,
        )
        self._state.poll_interval_ms = effective.config.polling.interval_ms
        self._state.max_concurrent_agents = effective.config.agent.max_concurrent_agents

    def _poll_reload(self) -> None:
        """Defensive per-tick reload check (SPEC §6.2)."""
        outcome = self._config_source.poll()
        if outcome is None:
            return
        if outcome.applied:
            logger.info("workflow reloaded %s", log_fields(outcome="completed"))
            self._apply_config(outcome.effective)
        # A failed reload kept the last known good config; the config source's
        # on_error callback owns the operator-visible message.

    # --- lifecycle -----------------------------------------------------------

    def serve(self) -> int:
        """Start up and run the event loop until stopped (SPEC §16.1).

        Returns:
            ``0`` after a graceful shutdown.

        Raises:
            DispatchPreflightError: Startup validation failed (SPEC §6.3);
                the host should treat this as a fatal startup error.
        """
        ensure_dispatchable(self._config)
        startup_terminal_workspace_cleanup(
            config=self._config,
            fetch_terminal_issues=lambda: self._tracker.fetch_issues_by_states(
                self._config.tracker.terminal_states
            ),
        )
        logger.info(
            "service started %s",
            log_fields(
                poll_interval_ms=self._state.poll_interval_ms,
                max_concurrent_agents=self._state.max_concurrent_agents,
            ),
        )
        self._serving = True
        self._events.put(_Tick())  # schedule_tick(delay_ms=0)
        try:
            while True:
                event = self._events.get()
                if isinstance(event, _Stop):
                    break
                self._handle(event)
        finally:
            self._serving = False
            self._shutdown()
        return 0

    def stop(self) -> None:
        """Request a graceful shutdown (thread-safe, idempotent)."""
        self._events.put(_Stop())

    def take_snapshot(self, *, timeout_ms: int = 2000) -> RuntimeSnapshot:
        """Build a runtime snapshot on the orchestrator thread (SPEC §13.3).

        Safe to call from any thread: the snapshot is built inside the event
        loop so it never races a state mutation, and the immutable result is
        handed back.

        Raises:
            SnapshotUnavailableError: The event loop is not running.
            SnapshotTimeoutError: The loop did not reply within ``timeout_ms``.
        """
        if not self._serving:
            raise SnapshotUnavailableError("service event loop is not running")
        reply: queue.SimpleQueue[RuntimeSnapshot] = queue.SimpleQueue()
        self._events.put(_SnapshotRequest(reply))
        try:
            return reply.get(timeout=timeout_ms / 1000.0)
        except queue.Empty as exc:
            raise SnapshotTimeoutError(
                f"snapshot request exceeded {timeout_ms} ms"
            ) from exc

    def _shutdown(self) -> None:
        """Graceful shutdown: cancel timers, wait briefly for workers.

        Worker threads cannot be interrupted mid-turn; they are given
        ``shutdown_grace_s`` in total to finish, then detached (they are
        daemon threads, so process exit does not wait for them).
        """
        if self._tick_timer is not None:
            self._tick_timer.cancel()
            self._tick_timer = None
        for entry in self._state.retry_attempts.values():
            timer = entry.timer_handle
            if isinstance(timer, threading.Timer):
                timer.cancel()

        deadline = time.monotonic() + self._shutdown_grace_s
        for issue_id, running in list(self._state.running.items()):
            worker = running.worker_handle
            if isinstance(worker, threading.Thread):
                worker.join(timeout=max(deadline - time.monotonic(), 0.0))
                if worker.is_alive():
                    logger.warning(
                        "worker still running at shutdown %s",
                        log_fields(
                            issue_id=issue_id,
                            issue_identifier=running.run_attempt.issue_identifier,
                        ),
                    )
        logger.info("service stopped %s", log_fields(outcome="completed"))

    # --- event handling ------------------------------------------------------

    def _handle(self, event: _Event) -> None:
        if isinstance(event, _Tick):
            self._on_tick()
        elif isinstance(event, _RetryDue):
            self._state = on_retry_timer(
                event.issue_id,
                self._state,
                fetch_active_issue_candidates=self._fetch_candidates,
                dispatch=self._dispatch,
                schedule_retry=self._scheduler,
            )
        elif isinstance(event, _WorkerExit):
            self._state = on_worker_exit(
                self._state,
                event.issue_id,
                event.result,
                schedule_retry=self._scheduler,
                now=self._now,
            )
        elif isinstance(event, _AgentUpdate):
            self._state = on_agent_update(
                self._state, event.issue_id, event.event, now=self._now
            )
        elif isinstance(event, _SnapshotRequest):
            event.reply.put(build_snapshot(self._state, now=self._now))

    def _on_tick(self) -> None:
        """One poll-and-dispatch tick, then schedule the next (SPEC §16.2)."""
        self._poll_reload()
        self._state = run_tick(
            self._state,
            policy=self._policy,
            reconcile_issues=self._reconcile,
            validate_dispatch_config=self._validate_dispatch,
            fetch_active_issue_candidates=self._fetch_candidates,
            dispatch=self._dispatch,
        )
        self._schedule_tick(self._state.poll_interval_ms)

    # --- orchestrator seam bindings -------------------------------------------

    def _validate_dispatch(self) -> bool:
        """Per-tick dispatch preflight (SPEC §6.3): log problems, never raise."""
        problems = check_dispatch_preflight(self._config)
        for problem in problems:
            logger.error(
                "dispatch validation failed %s",
                log_fields(error_code=problem.code, reason=problem.message),
            )
        return not problems

    def _fetch_candidates(self) -> list[Issue]:
        return self._tracker.fetch_candidate_issues()

    def _reconcile(self, state: OrchestratorState) -> OrchestratorState:
        return reconcile_running_issues(
            state,
            config=self._config,
            policy=self._policy,
            tracker=self._tracker,
            stop_worker=self._stop_worker,
            schedule_retry=self._scheduler,
            now=self._now,
        )

    def _dispatch(
        self, issue: Issue, state: OrchestratorState, attempt: int | None
    ) -> OrchestratorState:
        root = self._config.workspace.root
        if root is None:  # the resolver always sets one; defensive (SPEC §5.3.3)
            logger.error(
                "dispatch skipped %s",
                log_fields(
                    issue_id=issue.id,
                    issue_identifier=issue.identifier,
                    reason="workspace.root is not configured",
                ),
            )
            return state
        return dispatch_issue(
            issue,
            state,
            attempt,
            workspace_root=root,
            spawn_worker=self._spawn_worker,
            schedule_retry=self._scheduler,
            now=self._now,
        )

    def _spawn_worker(self, issue: Issue, attempt: int | None) -> object | None:
        """Run the attempt on a worker thread (SPEC §16.4-16.5).

        The worker gets the config + prompt snapshot in force at dispatch time
        (SPEC §6.2: reload applies to future runs) and reports back exclusively
        through the event queue.
        """
        config = self._config
        template = self._prompt_template
        tracker = self._tracker

        def work() -> None:
            try:
                result = self._run_attempt(
                    issue,
                    config=config,
                    prompt_template=template,
                    tracker=tracker,
                    attempt=attempt,
                    on_event=lambda ev: self._events.put(_AgentUpdate(issue.id, ev)),
                )
            except Exception as exc:  # run_agent_attempt's contract is to not
                # raise; this guard keeps a violation from silently leaking the
                # worker slot (the exit event must always arrive).
                logger.exception(
                    "worker crashed %s",
                    log_fields(issue_id=issue.id, issue_identifier=issue.identifier),
                )
                result = AttemptResult(
                    error=SymphonyError(f"worker crashed: {exc}"),
                    session_id=None,
                    turns=0,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    cost_usd=None,
                    final_state=None,
                )
            self._events.put(_WorkerExit(issue.id, result))

        thread = threading.Thread(
            target=work, name=f"symphony-worker-{issue.identifier}", daemon=True
        )
        try:
            thread.start()
        except RuntimeError:
            return None  # could not spawn -> dispatch routes to a retry
        return thread

    def _stop_worker(self, entry: RunningEntry) -> None:
        """Reconciliation asked to stop a worker (SPEC §8.5).

        Worker threads cannot be killed; the entry has already been removed
        from ``running``, so the thread's eventual exit event is a no-op, and
        the worker itself stops at the next turn boundary when it sees the
        issue is no longer active (SPEC §16.5).
        """
        logger.info(
            "worker stop requested %s",
            log_fields(
                issue_id=entry.run_attempt.issue_id,
                issue_identifier=entry.run_attempt.issue_identifier,
                session_id=entry.session.session_id if entry.session else None,
            ),
        )

    # --- timer bindings --------------------------------------------------------

    def _schedule_tick(self, delay_ms: int) -> None:
        timer = threading.Timer(delay_ms / 1000.0, lambda: self._events.put(_Tick()))
        timer.daemon = True
        timer.start()
        self._tick_timer = timer

    def _set_retry_timer(self, issue_id: str, delay_ms: int) -> object:
        timer = threading.Timer(
            delay_ms / 1000.0, lambda: self._events.put(_RetryDue(issue_id))
        )
        timer.daemon = True
        timer.start()
        return timer

    def _cancel_retry_timer(self, handle: object) -> None:
        if isinstance(handle, threading.Timer):
            handle.cancel()
