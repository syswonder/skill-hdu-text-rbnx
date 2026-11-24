# SPDX-License-Identifier: MIT
"""Text adapter tests: SDK Hershey ASCII, hanzi, errors, workspace shift."""

from __future__ import annotations

import pytest

from xyz_text.plan import Workspace
from xyz_text.text import TextError, TextOptions, layout_text


def _layout(text: str, *, workspace: Workspace | None = None, **kwargs):
    ws = workspace or Workspace()
    return layout_text(text, TextOptions(workspace=ws, **kwargs))


def _zs(points):
    return {z for _, _, z in points}


def test_ascii_produces_pen_up_and_down_points():
    points = _layout("AB", font_size_mm=10.0, origin_y=100.0)
    assert points
    assert all(len(point) == 3 for point in points)
    assert _zs(points) == {-46.0, -50.0}


def test_empty_text_produces_no_points():
    assert _layout("") == []


def test_newline_moves_the_next_baseline_down():
    single = _layout("A", font_size_mm=10.0, line_spacing=2.0, origin_y=100.0)
    double = _layout("A\nA", font_size_mm=10.0, line_spacing=2.0, origin_y=100.0)

    assert double[: len(single)] == single
    second_line = double[len(single):]
    assert second_line
    # Same glyph, translated down by one line height (10 * 2 = 20 mm).
    first_top = max(y for _, y, _ in single)
    second_top = max(y for _, y, _ in second_line)
    assert second_top == pytest.approx(first_top - 20.0)


def test_default_origin_is_top_aligned_and_inside_the_paper():
    points = _layout("A", font_size_mm=10.0, origin_y=None)
    ys = [y for _, y, _ in points]
    assert 0.0 <= min(ys) and max(ys) <= 150.0


def test_hanzi_are_supported_by_the_sdk_table():
    from xyz_text.geometry import hanzi_data

    if "你" not in hanzi_data.GLYPHS:
        pytest.skip("vendored hanzi table does not contain the test character")

    points = _layout("你好", font_size_mm=10.0, origin_y=100.0)
    assert points
    assert _zs(points) == {-46.0, -50.0}


def test_punctuation_is_rejected_with_a_clear_error():
    # The single-stroke font has no punctuation glyphs.
    with pytest.raises(TextError, match="不支持的字"):
        _layout("A!")


def test_font_size_below_the_sdk_minimum_is_rejected():
    with pytest.raises(TextError):
        _layout("A", font_size_mm=1.0)


def test_origin_x_outside_the_workspace_is_rejected():
    with pytest.raises(TextError, match="origin_x"):
        _layout("A", origin_x=500.0)


def test_text_that_does_not_fit_the_paper_is_rejected():
    ws = Workspace(x_min=0.0, x_max=20.0, y_min=0.0, y_max=15.0)
    with pytest.raises(TextError):
        _layout("AAAAAAAAAAAAAAAAAAAA", workspace=ws, font_size_mm=10.0)


def test_shifted_workspace_offsets_the_result():
    base = Workspace()
    shifted = Workspace(x_min=10.0, x_max=190.0, y_min=20.0, y_max=170.0)

    plain = _layout("A", workspace=base, origin_x=5.0, origin_y=100.0)
    moved = _layout("A", workspace=shifted, origin_x=15.0, origin_y=120.0)

    assert len(plain) == len(moved)
    for (px, py, pz), (mx, my, mz) in zip(plain, moved):
        assert (mx, my, mz) == pytest.approx((px + 10.0, py + 20.0, pz))