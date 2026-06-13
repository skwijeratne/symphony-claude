"""Integration tests for the composed service event loop (SPEC §16.1)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from symphony.agent_runner import AttemptResult
from symphony.exceptions import (
    DispatchPreflightError,
    LinearApiRequestError,
    SnapshotUnavailableError,
    SymphonyError,
)
from symphony.models import Issue
from symphony.reload import WorkflowConfigStore
from symphony.service import SymphonyService
from symphony.stream_parser import AgentEvent, AgentEventType

_WORKFLOW = """\
---
tracker:
  kind: linear
  api_key: lin_secret
  project_slug: my-team
polling:
  interval_ms: {interval}
agent:
  max_concurrent_agents: {max_agents}
workspace:
  root: {root}
---
Work on {{{{ issue.identifier }}}}.
"""


def _write_workflow(tmp_path: Path, *, interval: int = 50, max_agents: int = 5) -> Path:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(
        _WORKFLOW.format(interval=interval, max_agents=max_agents, root=tmp_path / "ws")
    )
    return path


def _issue(issue_id: str = "i-1", identifier: str = "ABC-1") -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title="Fix bug",
        state="Todo",
        url=f"https://linear.app/t/{identifier}",
    )


class _FakeTracker:
    """In-memory tracker: candidates are served once, then the queue is empty."""

    def __init__(self, candidates: list[Issue] | None = None) -> None:
        self.pending = list(candidates or [])
        self.candidate_calls = 0
        self.terminal_queries: list[list[str]] = []
        self.fail_candidates = False

    def fetch_candidate_issues(self) -> list[Issue]:
        self.candidate_calls += 1
        if self.fail_candidates:
            raise LinearApiRequestError("tracker down")
        served, self.pending = self.pending, []
        return served

    def fetch_issues_by_states(self, state_names: Sequence[str]) -> list[Issue]:
        self.terminal_queries.append(list(state_names))
        return []

    def fetch_issue_states_by_ids(self, issue_ids: Sequence[str]) -> list[Issue]:
        return []


def _success_result(session_id: str | None = "sess-1") -> AttemptResult:
    return AttemptResult(
        error=None,
        session_id=session_id,
        turns=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        cost_usd=0.25,
        final_state="Done",
    )


def _failed_result() -> AttemptResult:
    return AttemptResult(
        error=SymphonyError("agent turn error"),
        session_id="sess-1",
        turns=1,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cost_usd=None,
        final_state=None,
    )


class _FakeWorker:
    """A controllable ``run_agent_attempt`` stand-in.

    Records every call, optionally emits agent events through ``on_event``, and
    optionally blocks on ``release`` so tests can observe the running state.
    """

    def __init__(
        self,
        result: AttemptResult | None = None,
        *,
        events: list[AgentEvent] | None = None,
        block: bool = False,
    ) -> None:
        self.result = result if result is not None else _success_result()
        self.events = list(events or [])
        self.calls: list[dict[str, object]] = []
        self.release = threading.Event()
        if not block:
            self.release.set()

    def __call__(
        self,
        issue: Issue,
        *,
        config: object,
        prompt_template: str,
        tracker: object,
        attempt: int | None,
        on_event: Callable[[AgentEvent], None],
    ) -> AttemptResult:
        self.calls.append(
            {"issue_id": issue.id, "attempt": attempt, "template": prompt_template}
        )
        for event in self.events:
            on_event(event)
        assert self.release.wait(timeout=10), "test forgot to release the worker"
        return self.result


class _ServeHarness:
    """Runs ``serve()`` on a background thread and guarantees teardown."""

    def __init__(self, service: SymphonyService) -> None:
        self.service = service
        self.exit_code: int | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        self.exit_code = self.service.serve()

    def __enter__(self) -> _ServeHarness:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.service.stop()
        self._thread.join(timeout=10)
        assert not self._thread.is_alive(), "service did not shut down"


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _service(
    tmp_path: Path,
    *,
    tracker: _FakeTracker,
    worker: _FakeWorker,
    interval: int = 50,
    shutdown_grace_s: float = 5.0,
) -> SymphonyService:
    path = _write_workflow(tmp_path, interval=interval)
    return SymphonyService(
        WorkflowConfigStore(path),
        tracker_factory=lambda config: tracker,
        run_attempt=worker,
        shutdown_grace_s=shutdown_grace_s,
    )


# --- startup (SPEC §16.1) ---------------------------------------------------------
def test_serve_fails_startup_on_invalid_dispatch_config(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text("---\ntracker:\n  kind: linear\n---\nprompt\n")  # no api_key

    service = SymphonyService(
        WorkflowConfigStore(path),
        tracker_factory=lambda config: _FakeTracker(),
        run_attempt=_FakeWorker(),
    )

    with pytest.raises(DispatchPreflightError):
        service.serve()


def test_startup_runs_terminal_workspace_cleanup(tmp_path: Path) -> None:
    tracker = _FakeTracker()
    service = _service(tmp_path, tracker=tracker, worker=_FakeWorker())

    with _ServeHarness(service):
        assert _wait_for(lambda: bool(tracker.terminal_queries))

    # The sweep queried the configured terminal states (SPEC §8.6, §16.1) —
    # the workflow sets none, so the config-default set applies.
    assert {"Done", "Canceled"} <= set(tracker.terminal_queries[0])


def test_snapshot_is_unavailable_before_serving(tmp_path: Path) -> None:
    service = _service(tmp_path, tracker=_FakeTracker(), worker=_FakeWorker())
    with pytest.raises(SnapshotUnavailableError):
        service.take_snapshot()


# --- dispatch -> worker -> exit -> retry (SPEC §16.2-16.6) --------------------------
def test_candidate_is_dispatched_to_a_worker_and_requeued_on_success(
    tmp_path: Path,
) -> None:
    tracker = _FakeTracker([_issue()])
    worker = _FakeWorker(_success_result())
    service = _service(tmp_path, tracker=tracker, worker=worker)

    with _ServeHarness(service) as harness:
        # Worker ran with the dispatched issue and the workflow's prompt template.
        assert _wait_for(lambda: bool(worker.calls))
        assert worker.calls[0]["issue_id"] == "i-1"
        assert worker.calls[0]["attempt"] is None
        assert worker.calls[0]["template"] == "Work on {{ issue.identifier }}."

        # The clean exit folded accounting in and queued a continuation retry.
        def _continuation_queued() -> bool:
            snapshot = service.take_snapshot()
            return bool(snapshot.retrying) and snapshot.agent_totals.total_tokens == 15

        assert _wait_for(_continuation_queued)
        snapshot = service.take_snapshot()
        assert snapshot.retrying[0].issue_id == "i-1"
        assert snapshot.retrying[0].attempt == 1
        assert snapshot.agent_totals.cost_usd == 0.25
        assert not snapshot.running

    assert harness.exit_code == 0


def test_failed_worker_queues_a_failure_retry(tmp_path: Path) -> None:
    tracker = _FakeTracker([_issue()])
    worker = _FakeWorker(_failed_result())
    service = _service(tmp_path, tracker=tracker, worker=worker)

    with _ServeHarness(service):

        def _failure_queued() -> bool:
            snapshot = service.take_snapshot()
            return bool(snapshot.retrying) and snapshot.retrying[0].error is not None

        assert _wait_for(_failure_queued)
        row = service.take_snapshot().retrying[0]
        assert row.attempt == 1
        assert row.error == "worker exited: agent turn error"


def test_agent_events_update_the_live_session_view(tmp_path: Path) -> None:
    events = [
        AgentEvent(
            AgentEventType.SESSION_STARTED,
            raw={},
            session_id="sess-1",
            subtype="init",
        ),
        AgentEvent(
            AgentEventType.TURN_COMPLETED,
            raw={"usage": {"input_tokens": 10, "output_tokens": 5}},
            session_id="sess-1",
            subtype="success",
            is_terminal=True,
        ),
    ]
    tracker = _FakeTracker([_issue()])
    worker = _FakeWorker(_success_result(), events=events, block=True)
    service = _service(tmp_path, tracker=tracker, worker=worker)

    with _ServeHarness(service):

        def _session_visible() -> bool:
            snapshot = service.take_snapshot()
            return bool(snapshot.running) and snapshot.running[0].session_id is not None

        assert _wait_for(_session_visible)
        row = service.take_snapshot().running[0]
        assert row.session_id == "sess-1"
        assert row.turn_count == 1
        assert row.total_tokens == 15
        assert row.issue_url == "https://linear.app/t/ABC-1"
        worker.release.set()


def test_tracker_failure_skips_the_tick_and_polling_continues(tmp_path: Path) -> None:
    tracker = _FakeTracker([_issue()])
    tracker.fail_candidates = True
    worker = _FakeWorker()
    service = _service(tmp_path, tracker=tracker, worker=worker, interval=20)

    with _ServeHarness(service):
        # Multiple ticks happened despite every candidate fetch failing.
        assert _wait_for(lambda: tracker.candidate_calls >= 3)
        assert worker.calls == []
        assert service.take_snapshot().running == ()


# --- dynamic reload (SPEC §6.2) -----------------------------------------------------
def test_reload_reapplies_polling_and_concurrency(tmp_path: Path) -> None:
    tracker = _FakeTracker()
    service = _service(tmp_path, tracker=tracker, worker=_FakeWorker(), interval=20)
    path = tmp_path / "WORKFLOW.md"

    with _ServeHarness(service):
        assert _wait_for(lambda: tracker.candidate_calls >= 1)
        path.write_text(
            _WORKFLOW.format(interval=35, max_agents=3, root=tmp_path / "ws")
        )

        # The next tick polls the file and re-applies the new limits (SPEC §6.2).
        assert _wait_for(lambda: service._state.max_concurrent_agents == 3)
        assert service._state.poll_interval_ms == 35


def test_invalid_reload_keeps_the_service_running(tmp_path: Path) -> None:
    tracker = _FakeTracker()
    service = _service(tmp_path, tracker=tracker, worker=_FakeWorker(), interval=20)
    path = tmp_path / "WORKFLOW.md"

    with _ServeHarness(service):
        assert _wait_for(lambda: tracker.candidate_calls >= 1)
        calls_before_break = tracker.candidate_calls
        path.write_text("---\nbroken: [\n---\nprompt\n")  # unparseable YAML

        # Ticks keep coming on the last known good config (SPEC §6.2).
        assert _wait_for(lambda: tracker.candidate_calls >= calls_before_break + 2)
        assert service._state.max_concurrent_agents == 5


# --- graceful shutdown (SPEC §16.1) --------------------------------------------------
def test_stop_returns_promptly_while_a_worker_is_still_running(tmp_path: Path) -> None:
    tracker = _FakeTracker([_issue()])
    worker = _FakeWorker(block=True)
    service = _service(tmp_path, tracker=tracker, worker=worker, shutdown_grace_s=0.05)

    started = time.monotonic()
    with _ServeHarness(service) as harness:
        assert _wait_for(lambda: bool(worker.calls))
    elapsed = time.monotonic() - started

    assert harness.exit_code == 0
    assert elapsed < 5.0  # did not wait for the blocked worker
    worker.release.set()


def test_stop_is_safe_to_call_repeatedly(tmp_path: Path) -> None:
    service = _service(tmp_path, tracker=_FakeTracker(), worker=_FakeWorker())

    with _ServeHarness(service) as harness:
        service.stop()
        service.stop()

    assert harness.exit_code == 0
