# SPDX-License-Identifier: MIT
"""Validate SDK waypoints before they reach the primitive.

The SDK geometry layer already produces pen-up/down waypoints:

    each stroke: (x0, y0, z_up), (x0, y0, z_down), ... , (xlast, ylast, z_up)

This module does not re-plan strokes. It only does what must happen
before a hardware call:

  * reject non-finite coordinates;
  * reject points outside the configured workspace;
  * reject Z values outside the pen-height range;
  * drop consecutive duplicate points;
  * enforce ``max_points``;
  * compute the bounding box returned by the MCP tools.

Units are millimetres throughout. The drawable area and pen heights are
provided by the plotter primitive; this module holds no machine-specific
numbers.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

Point3 = tuple[float, float, float]


class PlanError(Exception):
    """Invalid workspace, out-of-bounds waypoint, empty plan, or too many points."""


@dataclass(frozen=True)
class Workspace:
    x_min: float = 0.0
    x_max: float = 0.0
    y_min: float = 0.0
    y_max: float = 0.0
    z_up: float = 0.0
    z_down: float = 0.0

    def validate(self) -> None:
        values = (
            self.x_min, self.x_max, self.y_min, self.y_max,
            self.z_up, self.z_down,
        )
        if not all(math.isfinite(value) for value in values):
            raise PlanError(f"workspace values must be finite, got {values}")
        if self.x_max <= self.x_min:
            raise PlanError(
                f"workspace x_max ({self.x_max}) must be > x_min ({self.x_min})"
            )
        if self.y_max <= self.y_min:
            raise PlanError(
                f"workspace y_max ({self.y_max}) must be > y_min ({self.y_min})"
            )
        if self.z_up <= self.z_down:
            raise PlanError(
                f"workspace z_up ({self.z_up}) must be > z_down ({self.z_down})"
            )

    def contains(self, x: float, y: float) -> bool:
        return self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max


@dataclass(frozen=True)
class WaypointSpec:
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class PlanResult:
    waypoints: list[WaypointSpec]
    min_x: float
    max_x: float
    min_y: float
    max_y: float
    point_count: int


def dedupe(points, tolerance: float = 1e-9) -> list[Point3]:
    """Drop consecutive duplicates and reject non-finite coordinates."""
    out: list[Point3] = []
    for raw in points:
        if len(raw) != 3:
            raise PlanError(f"waypoint must be (x, y, z), got {raw!r}")
        x, y, z = float(raw[0]), float(raw[1]), float(raw[2])
        if not all(math.isfinite(value) for value in (x, y, z)):
            raise PlanError(f"non-finite waypoint: ({x}, {y}, {z})")
        if not out or any(abs(a - b) > tolerance for a, b in zip((x, y, z), out[-1])):
            out.append((x, y, z))
    return out


def validate_waypoints(
    points,
    *,
    workspace: Workspace,
    max_points: int = 50000,
) -> PlanResult:
    """Validate SDK waypoints and wrap them in a :class:`PlanResult`.

    Raises :class:`PlanError` before any hardware call if a point escapes
    the workspace, Z escapes the pen-height range, the plan is empty, or
    it exceeds ``max_points``.
    """
    workspace.validate()

    limit = int(max_points)
    if limit < 1:
        raise PlanError(f"max_points must be >= 1, got {max_points}")

    cleaned = dedupe(points)
    if not cleaned:
        raise PlanError("no waypoints to draw")

    for x, y, z in cleaned:
        if not workspace.contains(x, y):
            raise PlanError(
                f"point ({x:.2f}, {y:.2f}) is outside workspace "
                f"x=[{workspace.x_min:.2f}, {workspace.x_max:.2f}] "
                f"y=[{workspace.y_min:.2f}, {workspace.y_max:.2f}]"
            )
        if z > workspace.z_up + 1e-6 or z < workspace.z_down - 1e-6:
            raise PlanError(
                f"z={z:.2f} is outside the pen-height range "
                f"[{workspace.z_down:.2f}, {workspace.z_up:.2f}]"
            )

    if len(cleaned) > limit:
        raise PlanError(
            f"plan has {len(cleaned)} waypoints, more than max_points ({limit}); "
            "lower the font size or split the text"
        )

    xs = [point[0] for point in cleaned]
    ys = [point[1] for point in cleaned]
    return PlanResult(
        waypoints=[WaypointSpec(x=x, y=y, z=z) for x, y, z in cleaned],
        min_x=min(xs),
        max_x=max(xs),
        min_y=min(ys),
        max_y=max(ys),
        point_count=len(cleaned),
    )


def describe_plan(plan: PlanResult) -> str:
    """One-line log summary: waypoint count plus first and last waypoint."""
    first = plan.waypoints[0]
    last = plan.waypoints[-1]
    return (
        f"{plan.point_count} waypoints, "
        f"start=({first.x:.2f}, {first.y:.2f}, {first.z:.2f}), "
        f"end=({last.x:.2f}, {last.y:.2f}, {last.z:.2f})"
    )