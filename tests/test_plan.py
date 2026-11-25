# SPDX-License-Identifier: MIT
"""Plan validation tests: bounds, pen heights, limits, dedupe, bbox."""

from __future__ import annotations

import math

import pytest

from write_text.plan import (
    PlanError,
    Workspace,
    dedupe,
    describe_plan,
    validate_waypoints,
    WaypointSpec,
)

WS = Workspace()


def _validate(points, **kwargs):
    return validate_waypoints(points, workspace=kwargs.pop("workspace", WS), **kwargs)


def test_bbox_covers_every_point():
    plan = _validate(
        [
            (10.0, 20.0, -46.0),
            (10.0, 20.0, -50.0),
            (30.0, 40.0, -50.0),
            (60.0, 5.0, -46.0),
        ]
    )
    assert (plan.min_x, plan.max_x) == (10.0, 60.0)
    assert (plan.min_y, plan.max_y) == (5.0, 40.0)
    assert plan.point_count == 4
    assert all(isinstance(point, WaypointSpec) for point in plan.waypoints)


def test_describe_plan_reports_count_and_endpoints():
    plan = _validate(
        [
            (10.0, 20.0, -46.0),
            (10.0, 20.0, -50.0),
            (30.0, 40.0, -50.0),
            (60.0, 5.0, -46.0),
        ]
    )
    assert describe_plan(plan) == (
        "4 waypoints, start=(10.00, 20.00, -46.00), end=(60.00, 5.00, -46.00)"
    )


def test_points_outside_the_workspace_are_rejected():
    with pytest.raises(PlanError, match="outside workspace"):
        _validate([(10.0, 10.0, -50.0), (181.0, 10.0, -50.0)])
    with pytest.raises(PlanError, match="outside workspace"):
        _validate([(10.0, -1.0, -50.0)])


def test_z_outside_the_pen_height_range_is_rejected():
    with pytest.raises(PlanError, match="pen-height"):
        _validate([(10.0, 10.0, -40.0)])
    with pytest.raises(PlanError, match="pen-height"):
        _validate([(10.0, 10.0, -51.0)])


def test_empty_plan_is_rejected():
    with pytest.raises(PlanError, match="no waypoints"):
        _validate([])


def test_non_finite_coordinates_are_rejected():
    with pytest.raises(PlanError, match="non-finite"):
        _validate([(10.0, 10.0, -50.0), (math.nan, 10.0, -50.0)])


def test_max_points_is_enforced_after_dedupe():
    points = [(10.0, 10.0, -50.0), (10.0, 10.0, -50.0), (20.0, 10.0, -50.0)]
    assert _validate(points, max_points=2).point_count == 2
    with pytest.raises(PlanError, match="max_points"):
        _validate(points, max_points=1)


def test_dedupe_drops_consecutive_duplicates_only():
    assert dedupe([(0.0, 0.0, -46.0), (0.0, 0.0, -46.0), (1.0, 1.0, -50.0)]) == [
        (0.0, 0.0, -46.0),
        (1.0, 1.0, -50.0),
    ]
    # A repeated point separated by another point is kept.
    assert len(dedupe([(0.0, 0.0, -46.0), (1.0, 0.0, -50.0), (0.0, 0.0, -46.0)])) == 3


def test_workspace_rejects_bad_pen_heights():
    with pytest.raises(PlanError, match="z_up"):
        Workspace(z_up=-50.0, z_down=-46.0).validate()
    with pytest.raises(PlanError, match="pen heights"):
        Workspace(z_up=1.0, z_down=-50.0).validate()


def test_workspace_rejects_empty_rectangles():
    with pytest.raises(PlanError, match="x_max"):
        Workspace(x_min=10.0, x_max=10.0).validate()
    with pytest.raises(PlanError, match="y_max"):
        Workspace(y_min=10.0, y_max=5.0).validate()