"""Dynamic ``WORKFLOW.md`` reload with last-known-good semantics (SPEC §6.2).

The configuration on disk can change while the service runs, and the service MUST
adjust without a restart (SPEC §6.2). This module owns that reload primitive:

* It composes the existing pipeline — :func:`~symphony.workflow_loader.load_workflow`
  (§5.1-5.2) and :func:`~symphony.config_resolver.resolve_config` (§6.1) — into a
  single re-read-and-re-apply step, exposing the result as one immutable
  :class:`EffectiveConfig` (the typed :class:`~symphony.config.ServiceConfig` plus
  the prompt template body) that future dispatch, retry, reconciliation, hooks, and
  agent launches read.
* On a reload that fails to load/parse/resolve, it keeps operating with the last
  known good effective configuration and surfaces an operator-visible error rather
  than crashing (SPEC §6.2, §17.1).
* It detects changes by file signature (mtime + size) so a caller can poll the file
  defensively — for example before each dispatch tick — in case a filesystem watch
  event was missed (SPEC §6.2). This polling-based detection *is* the change
  detector; a continuous OS-level watch loop is left to the host/event-loop wire-up
  (SPEC §16.1), which can drive :meth:`WorkflowReloader.poll` or
  :meth:`WorkflowReloader.reload` on whatever cadence it runs.

Scope boundary: a reload is rejected (last-known-good kept) only for the typed
:class:`~symphony.exceptions.WorkflowConfigError` family raised by loading and
resolution. Dispatch preflight (§6.3, :mod:`symphony.preflight`) is a *separate*
gate: a config that parses and resolves but is not dispatchable (for example a
removed ``tracker.api_key``) is still applied here — it changes polling cadence,
concurrency, states, etc. — and the per-tick preflight independently skips dispatch
for that tick. Keeping the two concerns apart matches the spec's split between §6.2
(reload must not crash) and §6.3 (preflight gates dispatch).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from symphony.config import ServiceConfig
from symphony.config_resolver import resolve_config
from symphony.exceptions import WorkflowConfigError
from symphony.workflow_loader import load_workflow

__all__ = ["EffectiveConfig", "ReloadOutcome", "WorkflowReloader"]


@dataclass(frozen=True, slots=True)
class EffectiveConfig:
    """The applied configuration the runtime reads (SPEC §6.2).

    A single immutable snapshot pairing the typed :class:`ServiceConfig` with the
    prompt template body from the same ``WORKFLOW.md``. Reload swaps the whole
    snapshot rather than mutating it, so a reader holding a reference always sees a
    consistent config + template pair.

    Attributes:
        config: The resolved, typed service configuration.
        prompt_template: The trimmed prompt body used to render per-issue prompts
            (SPEC §5.4); applies to *future* runs after a reload.
    """

    config: ServiceConfig
    prompt_template: str


@dataclass(frozen=True, slots=True)
class ReloadOutcome:
    """The result of a reload attempt (SPEC §6.2).

    Attributes:
        effective: The effective configuration in force *after* the attempt — the
            freshly applied one when ``applied`` is true, otherwise the retained
            last known good.
        applied: ``True`` when a newly loaded configuration was applied; ``False``
            when the attempt failed and the previous configuration was kept.
        error: The typed error that caused a failed attempt, or ``None`` on success.
            Present so callers can log an operator-visible message (SPEC §6.2).
    """

    effective: EffectiveConfig
    applied: bool
    error: WorkflowConfigError | None = None


# A signature that changes whenever the file content plausibly changed; ``None``
# when the file cannot be stat'd (missing/unreadable). mtime + size is cheap and
# does not read the file body (which may be a large prompt template).
_Signature = tuple[int, int] | None


class WorkflowReloader:
    """Holds the effective config and re-applies it on ``WORKFLOW.md`` changes.

    The initial load is strict: construction loads, resolves, and adopts the
    configuration, propagating a :class:`WorkflowConfigError` if it fails so startup
    can abort (SPEC §16.1). Once constructed there is always a last known good
    :class:`EffectiveConfig` to fall back to, and subsequent reloads never raise for
    a bad on-disk config — they keep the previous one (SPEC §6.2).
    """

    def __init__(
        self,
        path: Path | str,
        *,
        workflow_dir: Path | None = None,
        env: Mapping[str, str] | None = None,
        on_error: Callable[[WorkflowConfigError], None] | None = None,
    ) -> None:
        """Load the initial configuration and adopt it as last known good.

        Args:
            path: The resolved ``WORKFLOW.md`` path to watch and reload. Use
                :func:`~symphony.workflow_loader.resolve_workflow_path` to apply the
                §5.1 precedence before constructing the reloader.
            workflow_dir: Directory that relative ``workspace.root`` values resolve
                against (SPEC §6.1); defaults to the directory containing ``path``.
            env: Environment mapping for ``$VAR`` resolution; defaults to the live
                process environment. Passing ``None`` re-reads ``os.environ`` on each
                reload, so env changes are picked up alongside file changes.
            on_error: Optional callback invoked with the typed error whenever a
                reload is rejected and the last known good config is kept. This is
                the operator-visible error hook required by SPEC §6.2 until the
                structured logging layer (SPEC §13) is wired in.

        Raises:
            WorkflowConfigError: The initial load/parse/resolve failed; startup
                should treat this as a fatal configuration error (SPEC §16.1).
        """
        self._path = Path(path)
        self._workflow_dir = (
            workflow_dir if workflow_dir is not None else self._path.parent
        )
        self._env = env
        self._on_error = on_error
        self._signature: _Signature = self._stat_signature()
        # Strict initial load: a failure here propagates to fail startup.
        self._effective = self._load()

    @property
    def current(self) -> EffectiveConfig:
        """The effective configuration currently in force (last known good)."""
        return self._effective

    def reload(self) -> ReloadOutcome:
        """Re-read and re-apply ``WORKFLOW.md`` (SPEC §6.2).

        Always re-reads the file and refreshes the change signature, so calling
        :meth:`reload` directly forces a reload regardless of whether a change was
        detected. On success the new :class:`EffectiveConfig` is adopted; on a typed
        :class:`WorkflowConfigError` the previous configuration is kept, ``on_error``
        is invoked, and the error is returned rather than raised (SPEC §6.2, §17.1).

        Returns:
            The outcome, whose ``effective`` is the configuration in force after the
            call.
        """
        # Refresh the signature first so a poll() after a failed reload does not keep
        # re-attempting the same broken file every tick.
        self._signature = self._stat_signature()
        try:
            effective = self._load()
        except WorkflowConfigError as exc:
            if self._on_error is not None:
                self._on_error(exc)
            return ReloadOutcome(effective=self._effective, applied=False, error=exc)
        self._effective = effective
        return ReloadOutcome(effective=effective, applied=True, error=None)

    def poll(self) -> ReloadOutcome | None:
        """Reload only if the file changed since the last check (SPEC §6.2).

        Intended for the defensive re-validate-before-dispatch call site (SPEC §6.2):
        cheap to call every tick, and a no-op when nothing changed. Change is
        detected by file signature (mtime + size); a file that becomes unreadable
        counts as a change and triggers a reload, which then keeps the last known
        good config.

        Returns:
            ``None`` when no change was detected since the previous check; otherwise
            the :class:`ReloadOutcome` from the triggered :meth:`reload`.
        """
        signature = self._stat_signature()
        if signature == self._signature:
            return None
        return self.reload()

    def _load(self) -> EffectiveConfig:
        """Run the load + resolve pipeline into an :class:`EffectiveConfig`.

        Raises:
            WorkflowConfigError: Loading, parsing, or resolution failed.
        """
        definition = load_workflow(self._path)
        config = resolve_config(
            definition, workflow_dir=self._workflow_dir, env=self._env
        )
        return EffectiveConfig(
            config=config, prompt_template=definition.prompt_template
        )

    def _stat_signature(self) -> _Signature:
        """Return ``(mtime_ns, size)`` for the file, or ``None`` if it cannot stat."""
        try:
            stat = self._path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)
