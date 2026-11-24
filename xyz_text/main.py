# SPDX-License-Identifier: MIT
"""xyz_text — MCP skill for writing text with an XYZ pen plotter.

Owns ``robonix/skill/text/*``:

  * ``driver``     shared lifecycle contract for this skill.
  * ``write_text`` text -> single-stroke layout -> waypoints.

The vendored geometry package (``xyz_text/geometry/text.py``) does all
font work. This module orchestrates the plotter primitive over gRPC; it
never touches the PLC or the TCP protocol.

Image generation and image drawing live in ``skill-draw-rbnx``
(``robonix/skill/plot/*``). Homing and global motion parameters are
exposed by the plotter primitive (``robonix/primitive/plotter/{home,
set_params}``) as MCP tools, so this skill never overwrites SET_PARAM.

Coordinate conventions come from the plotter primitive: millimetres, paper
origin at the lower-left corner, +X right, +Y up. The drawable area and
pen heights are read from ``robonix/primitive/plotter/geometry`` once on
activation (falling back to the configured workspace).
"""
from __future__ import annotations

import dataclasses
import logging
import math
import time
from dataclasses import dataclass

from robonix_api import Deferred, Err, Ok, Skill

xyz_text = Skill(id="xyz_text", namespace="robonix/skill/text")

from plot_mcp import (  # type: ignore  # noqa: E402  (rbnx codegen)
    WriteText_Request,
    WriteText_Response,
)

from .plan import (  # noqa: E402
    PlanError,
    Workspace,
    describe_plan,
    validate_waypoints,
)
from .primitive_client import (  # noqa: E402
    PrimitiveCallError,
    PrimitiveClient,
    PrimitiveDiscoveryError,
    resolve_endpoints,
)
from .text import TextError, TextOptions, layout_text  # noqa: E402

log = logging.getLogger("xyz_text")
logging.basicConfig(
    level=logging.INFO,
    format="[xyz_text] %(levelname)s %(message)s",
)
# basicConfig is a no-op when the runtime already installed root handlers,
# so pin this logger's level explicitly to keep job logs visible.
log.setLevel(logging.INFO)


@dataclass(frozen=True)
class SkillConfig:
    workspace: Workspace
    font_size_mm: float
    line_spacing: float
    letter_spacing_mm: float
    origin_x: float
    #: Baseline of the first line; negative means "SDK default" (top-aligned).
    origin_y: float
    auto_home: bool
    resolve_timeout_s: float
    max_points: int
    trajectory_timeout_s: float


_state = {
    "config": None,
    "client": None,
    "active": False,
}


# ── config parsing ───────────────────────────────────────────────────
def _num(
    cfg: dict,
    key: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
):
    raw = cfg.get(key, default)
    try:
        value = int(raw) if integer else float(raw)
    except (TypeError, ValueError) as exc:
        kind = "an integer" if integer else "a number"
        raise ValueError(f"{key} must be {kind}, got {raw!r}") from exc
    if not integer and not math.isfinite(value):
        raise ValueError(f"{key} must be finite, got {value}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{key} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{key} must be <= {maximum}, got {value}")
    return value


def _bool(cfg: dict, key: str, default: bool) -> bool:
    raw = cfg.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


def _parse_config(cfg: dict) -> SkillConfig:
    raw_workspace = cfg.get("workspace") or {}
    if not isinstance(raw_workspace, dict):
        raise ValueError(
            f"workspace must be a mapping, got {type(raw_workspace).__name__}"
        )
    # The workspace geometry (drawable area + pen heights) is authoritative
    # in the plotter primitive and is fetched on activate. Config values are
    # neutral placeholders (all-zero) and are NOT validated here; the real
    # geometry replaces them once the primitive is reachable.
    workspace = Workspace(
        x_min=_num(raw_workspace, "x_min", 0.0),
        x_max=_num(raw_workspace, "x_max", 0.0),
        y_min=_num(raw_workspace, "y_min", 0.0),
        y_max=_num(raw_workspace, "y_max", 0.0),
        z_up=_num(raw_workspace, "z_up", 0.0),
        z_down=_num(raw_workspace, "z_down", 0.0),
    )

    return SkillConfig(
        workspace=workspace,
        font_size_mm=_num(cfg, "font_size_mm", 10.0, minimum=0.1),
        line_spacing=_num(cfg, "line_spacing", 1.5, minimum=1.0),
        letter_spacing_mm=_num(cfg, "letter_spacing_mm", 0.5, minimum=0.0),
        origin_x=_num(cfg, "origin_x", 5.0),
        origin_y=_num(cfg, "origin_y", -1.0),
        auto_home=_bool(cfg, "auto_home", False),
        resolve_timeout_s=_num(cfg, "resolve_timeout_s", 60.0, minimum=0.0),
        max_points=_num(cfg, "max_points", 50000, minimum=1, integer=True),
        trajectory_timeout_s=_num(
            cfg, "trajectory_timeout_s", 600.0, minimum=1.0
        ),
    )


def _config() -> SkillConfig:
    config = _state["config"]
    if config is None:
        raise RuntimeError("skill is not initialized")
    return config


def _client() -> PrimitiveClient:
    client = _state["client"]
    if client is None or not _state["active"]:
        raise RuntimeError("skill is not active; primitive endpoints are unresolved")
    return client


# ── request -> plan ──────────────────────────────────────────────────
def _workspace_from_request(req, fallback: Workspace) -> Workspace:
    raw = getattr(req, "workspace", None)
    if raw is None:
        return fallback
    x_min = float(raw.x_min)
    x_max = float(raw.x_max)
    y_min = float(raw.y_min)
    y_max = float(raw.y_max)
    # Default (all-zero) or malformed rectangles mean "use config".
    if x_max <= x_min or y_max <= y_min:
        return fallback
    z_up = float(raw.z_up)
    z_down = float(raw.z_down)
    if z_up <= z_down:
        z_up, z_down = fallback.z_up, fallback.z_down
    workspace = Workspace(
        x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max,
        z_up=z_up, z_down=z_down,
    )
    workspace.validate()
    return workspace


def _options_from_request(config: SkillConfig, req) -> TextOptions:
    font_size = float(req.font_size_mm)
    if font_size <= 0.0:
        font_size = config.font_size_mm

    line_spacing = float(req.line_spacing)
    if line_spacing <= 0.0:
        line_spacing = config.line_spacing

    # MCP dataclasses fill omitted numbers with 0, so 0 must mean "use the
    # configured default" here. An explicit zero letter spacing or origin is
    # therefore expressed by setting that value in the skill config.
    letter_spacing = float(req.letter_spacing_mm)
    if letter_spacing <= 0.0:
        letter_spacing = config.letter_spacing_mm

    origin_x = float(req.origin_x)
    if origin_x <= 0.0:
        origin_x = config.origin_x

    # Non-positive origin_y means "use config": a configured negative value
    # selects the SDK default (text block top-aligned at the workspace top).
    origin_y_raw = float(req.origin_y)
    if origin_y_raw <= 0.0:
        origin_y = config.origin_y if config.origin_y >= 0.0 else None
    else:
        origin_y = origin_y_raw

    return TextOptions(
        font_size_mm=font_size,
        line_spacing=line_spacing,
        letter_spacing_mm=letter_spacing,
        origin_x=origin_x,
        origin_y=origin_y,
        workspace=_workspace_from_request(req, config.workspace),
    )


def _write_response(success: bool, message: str, plan, started: float) -> WriteText_Response:
    return WriteText_Response(
        success=success,
        message=message,
        points=plan.point_count,
        min_x=plan.min_x,
        max_x=plan.max_x,
        min_y=plan.min_y,
        max_y=plan.max_y,
        elapsed_s=time.monotonic() - started,
    )


def _write_failure(message: str, started: float) -> WriteText_Response:
    return WriteText_Response(
        success=False,
        message=message,
        points=0,
        min_x=0.0,
        max_x=0.0,
        min_y=0.0,
        max_y=0.0,
        elapsed_s=time.monotonic() - started,
    )


# ── MCP tools ────────────────────────────────────────────────────────
@xyz_text.mcp("robonix/skill/text/write_text")
def write_text(req: WriteText_Request) -> WriteText_Response:
    """Write text on paper with the XYZ pen plotter.

    Supports uppercase/lowercase English letters, digits, spaces, newlines,
    and the 3755 common Chinese characters shipped with the SDK. Punctuation
    is not supported by the single-stroke font and returns an error. Use
    '\\n' for multiple lines. Sizes and coordinates are millimetres; the
    drawable area and pen heights are provided by the plotter primitive.

    Set dry_run=true to preview the waypoint count and bounding box without
    moving the machine. Use this for any request to write, sign, label, or
    print text. Image generation and drawing are separate tools in the
    image skill.
    """
    started = time.monotonic()
    text = str(req.text or "")
    if not text.strip():
        return _write_failure("text is empty", started)

    try:
        config = _config()
        options = _options_from_request(config, req)

        points = layout_text(text, options)
        if not points:
            return _write_failure("text produced no strokes", started)

        plan = validate_waypoints(
            points,
            workspace=options.workspace,
            max_points=config.max_points,
        )
        plan_summary = describe_plan(plan)

        if req.dry_run:
            log.info("write_text dry run: %s", plan_summary)
            return _write_response(
                True,
                f"dry run: {plan.point_count} waypoints planned, machine not moved",
                plan,
                started,
            )

        log.info("write_text sending: %s", plan_summary)
        client = _client()
        response = client.run_trajectory(
            plan.waypoints, timeout_s=config.trajectory_timeout_s
        )
        if not response.success:
            log.warning(
                "write_text primitive stopped after %d/%d points: %s",
                response.points_completed,
                response.points_accepted,
                response.message,
            )
            return _write_response(
                False,
                "primitive stopped after "
                f"{response.points_completed}/{response.points_accepted} points: "
                f"{response.message}",
                plan,
                started,
            )

        message = f"wrote {plan.point_count} waypoints"
        if req.auto_home or config.auto_home:
            home_response = client.home(timeout_s=config.trajectory_timeout_s)
            message += (
                "; returned home"
                if home_response.success
                else f"; auto home failed: {home_response.message}"
            )

        log.info("write_text ok: %s", plan_summary)
        return _write_response(True, message, plan, started)

    except (
        TextError,
        PlanError,
        PrimitiveCallError,
        PrimitiveDiscoveryError,
        RuntimeError,
        ValueError,
    ) as exc:
        log.warning("write_text failed: %s", exc)
        return _write_failure(str(exc), started)
    except Exception as exc:  # noqa: BLE001 - never raise across the MCP boundary
        log.exception("write_text raised unexpectedly")
        return _write_failure(str(exc) or exc.__class__.__name__, started)


# ── lifecycle ────────────────────────────────────────────────────────
@xyz_text.on_init
def init(cfg: dict):
    """REGISTERED -> INACTIVE. Parse and validate config only."""
    try:
        config = _parse_config(cfg or {})
    except (TypeError, ValueError, PlanError) as exc:
        return Err(f"invalid xyz_text config: {exc}")

    _state["config"] = config
    log.info(
        "init ok: workspace x[%.1f, %.1f] y[%.1f, %.1f] z_up=%.2f z_down=%.2f",
        config.workspace.x_min, config.workspace.x_max,
        config.workspace.y_min, config.workspace.y_max,
        config.workspace.z_up, config.workspace.z_down,
    )
    return Ok()


@xyz_text.on_activate
def activate():
    """INACTIVE -> ACTIVE. Resolve the plotter primitive endpoints."""
    config = _state["config"]
    if config is None:
        return Err("skill was not initialized")

    try:
        bindings = resolve_endpoints(
            xyz_text, timeout_s=config.resolve_timeout_s
        )
    except PrimitiveDiscoveryError as exc:
        # Stay INACTIVE so the next MCP call retries activation instead of
        # parking the skill in ERROR. The primitive is declared first in the
        # deploy manifest, so this is normally a boot-order race only.
        log.warning("activate deferred: %s", exc)
        return Deferred(str(exc))

    old, _state["client"] = _state["client"], None
    if old is not None:
        old.close()
    client = PrimitiveClient(
        bindings, default_timeout_s=config.trajectory_timeout_s
    )
    _state["client"] = client
    try:
        geo = client.get_geometry()
        _state["config"] = dataclasses.replace(
            config,
            workspace=Workspace(
                x_min=geo.x_min, x_max=geo.x_max,
                y_min=geo.y_min, y_max=geo.y_max,
                z_up=geo.z_up, z_down=geo.z_down,
            ),
        )
        log.info(
            "geometry from primitive: x_max=%.1f y_max=%.1f z_up=%.2f z_down=%.2f",
            geo.x_max, geo.y_max, geo.z_up, geo.z_down,
        )
    except Exception as exc:  # noqa: BLE001 - activation must never raise
        # Degrade instead of failing: a provider stuck in ERROR cannot be
        # activated again until the whole stack restarts.
        usable = (
            config.workspace.x_max > config.workspace.x_min
            and config.workspace.y_max > config.workspace.y_min
        )
        if usable:
            log.warning("geometry unavailable; using configured workspace: %s", exc)
        else:
            log.error(
                "geometry unavailable and no workspace configured; "
                "drawing will fail until one is set: %s", exc,
            )
    _state["active"] = True
    log.info(
        "activated: trajectory=%s home=%s",
        bindings["trajectory"].endpoint,
        bindings["home"].endpoint,
    )
    return Ok()


@xyz_text.on_deactivate
def deactivate():
    """ACTIVE -> INACTIVE. Drop the primitive gRPC channels."""
    _state["active"] = False
    client, _state["client"] = _state["client"], None
    if client is not None:
        client.close()
    log.info("deactivated")
    return Ok()


@xyz_text.on_shutdown
def shutdown():
    deactivate()
    log.info("shutdown")
    return Ok()


def main() -> int:
    xyz_text.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())