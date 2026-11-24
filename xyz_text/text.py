# SPDX-License-Identifier: MIT
"""Text layout backed by the vendored geometry package.

The geometry package (``xyz_text/geometry/text.py``) does the real work:

    ASCII letters/digits -> Hershey Simplex single-stroke glyphs
    Chinese              -> stroke medians for the 3755 GB2312 level-1 chars
    layout                -> wrapping, overflow checks, cursor, pen up/down

This module only adapts the skill's ``Workspace`` / options to the
geometry module's ``TextConfig`` and returns absolute ``(x, y, z)``
waypoints ready for the primitive trajectory contract.

Supported characters: ASCII ``A-Z a-z 0-9``, space, newline, and the
hanzi in ``hanzi_data.py``. Punctuation is not in the font tables and
raises :class:`TextError`.
"""
from __future__ import annotations

from dataclasses import dataclass

from .plan import PlanError, Workspace
from .geometry import text as text_module


class TextError(Exception):
    """Invalid text options, unsupported character, or overflow."""


@dataclass(frozen=True)
class TextOptions:
    font_size_mm: float = 10.0
    line_spacing: float = 1.5
    letter_spacing_mm: float = 0.5
    origin_x: float = 5.0
    #: Baseline of the first line, in machine mm. ``None`` means "use the
    #: SDK default": the text block's top edge sits at the workspace top.
    origin_y: float | None = None
    workspace: Workspace = Workspace()


def _sdk_config(options: TextOptions, width: float, height: float, origin_x: float):
    try:
        cfg = text_module.TextConfig(
            font_size_mm=float(options.font_size_mm),
            line_spacing=float(options.line_spacing),
            letter_spacing_mm=float(options.letter_spacing_mm),
            work_width_mm=float(width),
            work_height_mm=float(height),
            margin_mm=0.0,
            origin_x_mm=float(origin_x),
            reset_y_mm=float(height),
            z_up=float(options.workspace.z_up),
            z_down=float(options.workspace.z_down),
        )
        cfg.validate()
    except ValueError as exc:
        raise TextError(str(exc)) from exc
    return cfg


def layout_text(text: str, options: TextOptions) -> list[tuple[float, float, float]]:
    """Lay ``text`` out and return absolute ``(x, y, z)`` waypoints."""
    workspace = options.workspace
    try:
        workspace.validate()
    except PlanError as exc:
        raise TextError(str(exc)) from exc

    if not text:
        return []

    width = workspace.x_max - workspace.x_min
    height = workspace.y_max - workspace.y_min
    # SDK configuration is 0-based; the workspace may be shifted.
    origin_x = float(options.origin_x) - workspace.x_min
    if not (0.0 <= origin_x <= width):
        raise TextError(
            f"origin_x must be within the workspace x range "
            f"[{workspace.x_min}, {workspace.x_max}], got {options.origin_x}"
        )

    cfg = _sdk_config(options, width, height, origin_x)
    cursor = None
    if options.origin_y is not None:
        baseline = float(options.origin_y) - workspace.y_min
        cfg.reset_y_mm = None
        cursor = text_module.Cursor(origin_x, baseline)

    try:
        points, _ = text_module.layout_text(text, cfg, cursor)
    except ValueError as exc:  # includes TextOverflowError
        raise TextError(str(exc)) from exc

    if workspace.x_min or workspace.y_min:
        dx, dy = workspace.x_min, workspace.y_min
        points = [(x + dx, y + dy, z) for x, y, z in points]
    return points