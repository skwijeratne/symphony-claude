"""``WORKFLOW.md`` discovery, front-matter/body split, and parsing (SPEC §5.1-5.2).

This is the first stage of configuration resolution: it turns a ``WORKFLOW.md``
file on disk into a :class:`~symphony.models.WorkflowDefinition` (raw front-matter
map + trimmed prompt body). Typing, defaults, ``$VAR``/``~`` resolution, and
validation of that map are the config layer's job (SPEC §6), handled by a later
PR.

Errors use the typed workflow/config hierarchy (SPEC §5.5): an unreadable file is
``missing_workflow_file``, malformed YAML (or unterminated front matter) is
``workflow_parse_error``, and front matter that does not decode to a map is
``workflow_front_matter_not_a_map``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from symphony.exceptions import (
    MissingWorkflowFileError,
    WorkflowFrontMatterNotAMapError,
    WorkflowParseError,
)
from symphony.models import WorkflowDefinition

__all__ = ["resolve_workflow_path", "load_workflow"]

_DEFAULT_WORKFLOW_FILENAME = "WORKFLOW.md"
_FENCE = "---"


def resolve_workflow_path(
    explicit_path: Path | str | None = None,
    *,
    cwd: Path | None = None,
) -> Path:
    """Resolve which ``WORKFLOW.md`` to load (SPEC §5.1).

    Precedence:
        1. ``explicit_path`` (set by CLI startup), used as given.
        2. ``WORKFLOW.md`` in ``cwd`` (the process working directory by default).

    Args:
        explicit_path: An explicit runtime path, or ``None`` to use the default.
        cwd: Base directory for the default; defaults to the current working
            directory. Primarily a testing seam.

    Returns:
        The path to load.
    """
    if explicit_path is not None:
        return Path(explicit_path)
    base = cwd if cwd is not None else Path.cwd()
    return base / _DEFAULT_WORKFLOW_FILENAME


def _split_front_matter(text: str) -> tuple[str | None, str]:
    """Split raw file text into front matter and body (SPEC §5.2).

    Front matter exists only when the first line is exactly ``---`` and a later
    line is exactly ``---``. The text between the fences is returned (unparsed)
    and the remainder is the body.

    Args:
        text: Full file contents.

    Returns:
        A ``(front_matter, body)`` tuple. ``front_matter`` is ``None`` when the
        file has no front matter, in which case the whole file is the body.

    Raises:
        WorkflowParseError: If an opening ``---`` fence has no closing fence.
    """
    first_newline = text.find("\n")
    first_line = text if first_newline == -1 else text[:first_newline]
    if first_line.strip() != _FENCE:
        return None, text

    # Scan line by line for the closing fence, but only across the front-matter
    # region: once it is found the body is sliced in one shot and never tokenized
    # (it can be a large prompt template).
    fm_start = first_newline + 1 if first_newline != -1 else len(text)
    pos = fm_start
    while pos < len(text):
        newline = text.find("\n", pos)
        if newline == -1:
            if text[pos:].strip() == _FENCE:
                return text[fm_start:pos], ""
            break
        if text[pos:newline].strip() == _FENCE:
            return text[fm_start:pos], text[newline + 1 :]
        pos = newline + 1
    raise WorkflowParseError(
        "unterminated YAML front matter: opening '---' has no closing '---'"
    )


def load_workflow(
    path: Path | str | None = None,
    *,
    cwd: Path | None = None,
) -> WorkflowDefinition:
    """Load and parse a ``WORKFLOW.md`` into a :class:`WorkflowDefinition`.

    Args:
        path: Explicit workflow path, or ``None`` to use the §5.1 default.
        cwd: Base directory for the default path; primarily a testing seam.

    Returns:
        The parsed workflow: front-matter ``config`` map (empty when absent) and
        the trimmed ``prompt_template`` body.

    Raises:
        MissingWorkflowFileError: The file cannot be read.
        WorkflowParseError: The front matter is not valid YAML or is unterminated.
        WorkflowFrontMatterNotAMapError: The front matter does not decode to a map.
    """
    resolved = resolve_workflow_path(path, cwd=cwd)
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise MissingWorkflowFileError(
            f"cannot read workflow file: {resolved}"
        ) from exc

    front_matter, body = _split_front_matter(text)
    config: dict[str, Any] = {}
    if front_matter is not None:
        try:
            parsed = yaml.safe_load(front_matter)
        except yaml.YAMLError as exc:
            raise WorkflowParseError(
                f"invalid YAML front matter in {resolved}: {exc}"
            ) from exc
        if parsed is None:
            # Empty front matter is treated as no config, not an error.
            config = {}
        elif isinstance(parsed, dict):
            config = parsed
        else:
            raise WorkflowFrontMatterNotAMapError(
                f"front matter must be a map/object, got {type(parsed).__name__}"
            )

    return WorkflowDefinition(config=config, prompt_template=body.strip())
