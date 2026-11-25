"""单线笔画文字排版与书写。

工作坐标：
    - 纸张左下角为 (0, 0)
    - +X 向右，+Y 向上
    - 默认从左上角开始写
    - 落笔 z_down、抬笔 z_up（由调用方从 plotter primitive 传入）
    - 写完回 HOME，即回到待机位

字形来源（两套，都是单线，共用同一条基线）：

    - 西文 / 数字：Hershey Simplex（futural），见 hershey_simplex.py；
    - 汉字：笔画中线（medians），见 hanzi_data.py。

汉字一笔一条折线，笔画之间抬笔跳转，所以画出来是单线字，不是空心轮廓。
汉字墨迹高度 = 字号，字格宽约 1.17 倍字号，字距固定为 CJK_TRACKING_MM。

排版策略（一次 layout 内）：
    - 每行都从 origin_x_mm 起笔（默认 5 mm），换行只把基线减一个行高；
    - 游标停在行尾，下次运行接着同一行写；--new-line 才落到下一行行首；
    - 越界、放不下都在**发送 TCP 之前**抛异常，不会下发半截。

本模块只做排版，不碰网络：下发由 RobonixClient（或 Robonix primitive）负责。

"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import hanzi_data
from . import hershey_simplex as font

# ---------------------------------------------------------------------------
# 配置 / 游标 / 异常
# ---------------------------------------------------------------------------
DEFAULT_STATE_PATH = Path.home() / ".robonix" / "writer_state.json"

# 汉字墨迹普遍比大写字母最高点再高一点点（实测「年」最高 0.51 单位），
# 排版留出的顶部余量用这个常量兜住。
TOP_SAFETY_UNITS = 1.0

# 调试钩子：layout_text 每落一个字前回调 (ch, 该字起点 x_mm, 基线 y_mm)。
# None 表示不记录；CLI 的 --debug-lines 用它打印逐行首字坐标。
emit_hook: Callable[[str, float, float], None] | None = None

@dataclass
class TextConfig:
    font_size_mm: float = 10.0       # 大写字母高度（cap height）；汉字墨迹高同此值
    line_spacing: float = 1.5        # 行高 = font_size_mm * line_spacing
    letter_spacing_mm: float = 0.5   # 字间距
    space_advance_mm: float | None = None  # 空格宽度；None = 0.6 * font_size
    # Drawable area, supplied by the caller from the primitive's drawable
    # area; there are no machine-specific defaults here.
    work_width_mm: float = 0.0
    work_height_mm: float = 0.0
    margin_mm: float = 0.0
    origin_x_mm: float = 5.0         # 段落左边界（绝对 X）：每行首字的字格左边缘
    reset_y_mm: float | None = None  # reset 后文字块顶边的绝对 Y；None = 纸高 - margin
    # Pen heights, supplied by the caller from the primitive's geometry contract.
    z_up: float = 0.0
    z_down: float = 0.0
    vel: float = 20.0
    acc: float = 300.0
    dec: float = 300.0
    jerk: float = 3000.0

    def validate(self) -> None:
        if self.work_width_mm <= 0 or self.work_height_mm <= 0:
            raise ValueError("工作范围必须大于 0")
        if self.font_size_mm < 2.0:
            raise ValueError(
                "字号 %.2f mm 太小：低于 2 mm 时笔画间隙小于笔宽，画不清"
                % self.font_size_mm
            )
        upper = self.work_height_mm - 2.0
        if self.font_size_mm > upper:
            raise TextOverflowError(
                "字号 %.2f mm 放不进纸高：上限 %.2f mm（纸高 %.1f mm - 2 mm）"
                % (self.font_size_mm, upper, self.work_height_mm)
            )
        if self.line_spacing <= 0:
            raise ValueError("line_spacing 必须大于 0")
        if self.margin_mm < 0:
            raise ValueError("margin_mm 不能为负")
        if not (0.0 <= self.origin_x_mm <= self.work_width_mm):
            raise ValueError(
                "origin_x_mm 必须在 [0, %.1f] 之间，当前 %.1f"
                % (self.work_width_mm, self.origin_x_mm)
            )
        if self.reset_y_mm is not None and not (
            0.0 < self.reset_y_mm <= self.work_height_mm + 1e-9
        ):
            raise ValueError(
                "reset_y_mm 必须在 (0, %.1f] 之间，当前 %s"
                % (self.work_height_mm, self.reset_y_mm)
            )

    @property
    def scale(self) -> float:
        """字体单位 -> mm。"""
        return self.font_size_mm / font.CAP_HEIGHT_UNITS

    @property
    def line_height_mm(self) -> float:
        return self.font_size_mm * self.line_spacing

    @property
    def space_advance(self) -> float:
        if self.space_advance_mm is not None:
            return self.space_advance_mm
        return 0.6 * self.font_size_mm


@dataclass
class Cursor:
    """当前书写位置（基线坐标，单位 mm）。"""

    x_mm: float
    y_mm: float

    def to_dict(self) -> dict:
        return {"x_mm": self.x_mm, "y_mm": self.y_mm}


class TextOverflowError(ValueError):
    """文字超出纸张范围。"""


def default_cursor(cfg: TextConfig) -> Cursor:
    """默认游标：文字块左上角。

    横向贴左边距；纵向由 ``reset_y_mm`` 决定（文字块顶边的绝对 Y，
    默认取纸高 —— 即从纸张左下角起点往 +Y 走的可绘高度）。
    基线在顶边下方 ``(BASELINE_Y_UNITS - TOP_UNITS + TOP_SAFETY_UNITS) * scale`` 处。
    """
    top_units = _TOP_UNITS
    drop = (font.BASELINE_Y_UNITS - top_units + TOP_SAFETY_UNITS) * cfg.scale
    top_y = (
        cfg.work_height_mm - cfg.margin_mm
        if cfg.reset_y_mm is None
        else cfg.reset_y_mm
    )
    return Cursor(cfg.origin_x_mm, top_y - drop)


# ---------------------------------------------------------------------------
# 字形解析
# ---------------------------------------------------------------------------
_STROKE_CACHE: dict[str, list[list[tuple[float, float]]]] = {}


def glyph_strokes(ch: str) -> list[list[tuple[float, float]]]:
    """把 "M x,y L x,y ..." 解析成若干条折线。"""
    if ch in _STROKE_CACHE:
        return _STROKE_CACHE[ch]
    if ch not in font.GLYPHS:
        raise ValueError("不支持的字符: %r（西文仅 Hershey 字表，汉字见 hanzi_data）" % ch)

    path = font.GLYPHS[ch][0]
    strokes: list[list[tuple[float, float]]] = []
    for part in path.split("M")[1:]:
        nums = [float(v) for v in re.findall(r"-?\d+\.?\d*", part)]
        pts = list(zip(nums[0::2], nums[1::2]))
        if len(pts) >= 2:
            strokes.append(pts)
    _STROKE_CACHE[ch] = strokes
    return strokes


# 汉字：字距固定 1.0 mm（加在字格宽上，末字后也多走这一小段）
CJK_TRACKING_MM = 1.0

# 源数据字格边长（见 hanzi_data.py 头注释）
CJK_EM_UNITS = 1024.0

_CJK_STROKE_CACHE: dict[str, list[list[tuple[float, float]]]] = {}


def is_cjk(ch: str) -> bool:
    """是否在收录的汉字字库里。"""
    return ch in hanzi_data.GLYPHS


def _cjk_scale_units() -> float:
    """源单位 -> Hershey 单位（等比）。

    让全字库最高的一笔正好落在 Hershey 的 cap 顶（y = 1），
    即汉字墨迹高度 = 字号，与西文大写字母等高。
    """
    return font.CAP_HEIGHT_UNITS / hanzi_data.Y_MAX


def _cjk_strokes(ch: str) -> list[list[tuple[float, float]]]:
    """汉字笔画（Hershey 单位：y 向下、基线 22），一条折线一笔。"""
    if ch not in _CJK_STROKE_CACHE:
        k = _cjk_scale_units()
        strokes = []
        for part in hanzi_data.GLYPHS[ch].split("M")[1:]:
            nums = [float(v) for v in re.findall(r"-?\d+\.?\d*", part)]
            pts = list(zip(nums[0::2], nums[1::2]))
            if len(pts) >= 2:
                strokes.append(
                    [(gx * k, font.BASELINE_Y_UNITS - gy * k) for gx, gy in pts]
                )
        _CJK_STROKE_CACHE[ch] = strokes
    return _CJK_STROKE_CACHE[ch]


def _strokes(ch: str) -> list[list[tuple[float, float]]]:
    """字形折线（Hershey 单位）。西文用 Hershey，汉字用笔画中线。"""
    if ch in hanzi_data.GLYPHS:
        return _cjk_strokes(ch)
    return glyph_strokes(ch)


def cjk_width_mm(cfg: TextConfig) -> float:
    """汉字字格宽 (mm) = 1024 源单位折算，约 1.2 倍字号。"""
    return CJK_EM_UNITS * _cjk_scale_units() * cfg.scale


def glyph_advance_units(ch: str) -> float:
    if ch in font.GLYPHS:
        return font.GLYPHS[ch][1] * font.ADVANCE_SCALE
    raise ValueError("不支持的字符: %r" % ch)


def glyph_advance_mm(ch: str, cfg: TextConfig) -> float:
    if ch == " ":
        return cfg.space_advance
    if ch in hanzi_data.GLYPHS:
        return cjk_width_mm(cfg) + CJK_TRACKING_MM
    return glyph_advance_units(ch) * cfg.scale + cfg.letter_spacing_mm


def _descender_units() -> float:
    max_y = font.BASELINE_Y_UNITS
    for ch in font.GLYPHS:
        for stroke in glyph_strokes(ch):
            for _, gy in stroke:
                if gy > max_y:
                    max_y = gy
    return max_y - font.BASELINE_Y_UNITS


_DESCENDER_MM_UNITS = _descender_units()


def _top_units() -> float:
    min_y = font.BASELINE_Y_UNITS
    for ch in font.GLYPHS:
        for stroke in glyph_strokes(ch):
            for _, gy in stroke:
                if gy < min_y:
                    min_y = gy
    return min_y


_TOP_UNITS = _top_units()


# ---------------------------------------------------------------------------
# 排版
# ---------------------------------------------------------------------------
def _emit_glyph(
    ch: str,
    x0: float,
    baseline: float,
    cfg: TextConfig,
    points: list[tuple[float, float, float]],
) -> None:
    if emit_hook is not None:
        emit_hook(ch, x0, baseline)
    scale = cfg.scale
    for stroke in _strokes(ch):
        world = [
            (x0 + gx * scale, baseline + (font.BASELINE_Y_UNITS - gy) * scale)
            for gx, gy in stroke
        ]
        points.append((world[0][0], world[0][1], cfg.z_up))
        points.append((world[0][0], world[0][1], cfg.z_down))
        for x, y in world[1:]:
            points.append((x, y, cfg.z_down))
        points.append((world[-1][0], world[-1][1], cfg.z_up))


def _iter_tokens(line: str):
    """把一行切成 token：("space" | "cjk" | "word", 文本)。

    汉字逐字成 token（任意字间都能断行），西文按空格成词（整词不拆）。
    """
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == " ":
            j = i
            while j < n and line[j] == " ":
                j += 1
            yield "space", line[i:j]
            i = j
        elif ch in hanzi_data.GLYPHS:
            yield "cjk", ch
            i += 1
        else:
            j = i
            while j < n and line[j] != " " and line[j] not in hanzi_data.GLYPHS:
                j += 1
            yield "word", line[i:j]
            i = j


def layout_text(
    text: str,
    cfg: TextConfig,
    cursor: Cursor | None = None,
) -> tuple[list[tuple[float, float, float]], Cursor]:
    """把字符串排版成轨迹点，并返回新的游标。

    自动换行在发送 TCP 之前完成；如果整体超出纸张，抛 ``TextOverflowError``。
    """
    cfg.validate()
    if cursor is None:
        cursor = default_cursor(cfg)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for ch in text:
        if ch in "\n ":
            continue
        if ch in font.GLYPHS or ch in hanzi_data.GLYPHS:
            continue
        raise ValueError(
            "不支持的字: %r（汉字仅限字库内的 %d 字）" % (ch, len(hanzi_data.GLYPHS))
        )

    scale = cfg.scale
    min_x = cfg.origin_x_mm          # 行首 / 换行后的左边界
    max_x = cfg.work_width_mm - cfg.margin_mm
    min_y = cfg.margin_mm
    max_y = cfg.work_height_mm - cfg.margin_mm
    descender_mm = _DESCENDER_MM_UNITS * scale

    x = float(cursor.x_mm)
    baseline = float(cursor.y_mm)
    points: list[tuple[float, float, float]] = []

    # 段落左边界：本段每一行都从同一个 x 起笔（接续写时 = 上次停下的 x）。
    # 若游标右侧余量已不足一个字，就从左边距另起一行，避免在右端反复换行。
    if x > max_x - cfg.font_size_mm + 1e-9:
        x = min_x
        baseline -= cfg.line_height_mm
    para_x = max(min_x, x)

    def wrap() -> None:
        nonlocal x, baseline
        x = para_x
        baseline -= cfg.line_height_mm
        if baseline - descender_mm < min_y - 1e-9:
            raise TextOverflowError(
                "文字超出纸张底部：需要至少再增加 %.2f mm 高度"
                % (min_y - (baseline - descender_mm))
            )

    # 起点已经在右边界外，先换行
    if x > max_x + 1e-9:
        wrap()

    lines = text.split("\n")
    for line_idx, line in enumerate(lines):
        for kind, token in _iter_tokens(line):
            if kind == "space":
                adv = cfg.space_advance * len(token)
                if x + adv > max_x + 1e-9 and x > 0:
                    wrap()
                else:
                    x += adv
                continue

            # 汉字：逐字断行，放不下就换行（不需要空格）
            if kind == "cjk":
                adv = glyph_advance_mm(token, cfg)
                if x + adv > max_x + 1e-9 and x > 0:
                    wrap()
                if x + adv > max_x + 1e-9:
                    raise TextOverflowError(
                        "汉字 %r 在字号 %.2f mm 下放不进一行（最大宽度 %.2f mm）"
                        % (token, cfg.font_size_mm, max_x)
                    )
                _emit_glyph(token, x, baseline, cfg, points)
                x += adv
                continue

            # 先判断整个单词是否放得下；放不下就在单词前换行
            word_adv = sum(glyph_advance_mm(ch, cfg) for ch in token)
            if x + word_adv > max_x + 1e-9 and x > 0:
                wrap()

            for ch in token:
                adv = glyph_advance_mm(ch, cfg)

                if x + adv > max_x + 1e-9 and x > 0:
                    wrap()

                if x + adv > max_x + 1e-9:
                    raise TextOverflowError(
                        "字符 %r 在字号 %.2f mm 下放不进一行（最大宽度 %.2f mm）"
                        % (ch, cfg.font_size_mm, max_x)
                    )

                _emit_glyph(ch, x, baseline, cfg, points)
                x += adv

        if line_idx < len(lines) - 1:
            wrap()

    if points:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        if min(xs) < min_x - 1e-6 or max(xs) > max_x + 1e-6:
            raise TextOverflowError(
                "文字超出纸张 X 范围：x=[%.3f, %.3f]，允许 [%.3f, %.3f]"
                % (min(xs), max(xs), min_x, max_x)
            )
        if min(ys) < min_y - 1e-6 or max(ys) > max_y + 1e-6:
            raise TextOverflowError(
                "文字超出纸张 Y 范围：y=[%.3f, %.3f]，允许 [%.3f, %.3f]"
                % (min(ys), max(ys), min_y, max_y)
            )

    return points, Cursor(x, baseline)


def text_to_waypoints(
    text: str,
    cfg: TextConfig | None = None,
    cursor: Cursor | None = None,
) -> tuple[list[tuple[float, float, float]], Cursor]:
    """便捷入口：``layout_text`` 的别名。"""
    return layout_text(text, cfg or TextConfig(), cursor)


# ---------------------------------------------------------------------------
# 游标持久化
# ---------------------------------------------------------------------------
def load_cursor(path: str | os.PathLike | None, cfg: TextConfig) -> Cursor:
    p = Path(path) if path is not None else DEFAULT_STATE_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        cur = Cursor(float(data["x_mm"]), float(data["y_mm"]))
    except (OSError, ValueError, KeyError, TypeError):
        return default_cursor(cfg)

    # 状态文件里的位置明显越界时，回到默认起点
    if not (0.0 <= cur.x_mm <= cfg.work_width_mm and 0.0 <= cur.y_mm <= cfg.work_height_mm):
        return default_cursor(cfg)

    # 基线高于默认首行基线时，首行字会顶出纸张（旧状态文件的纸张尺寸
    # 比当前可绘区域更大时就会触发），同样回到默认起点。
    if cur.y_mm > default_cursor(cfg).y_mm + 1e-6:
        return default_cursor(cfg)
    return cur


def save_cursor(path: str | os.PathLike | None, cursor: Cursor) -> None:
    p = Path(path) if path is not None else DEFAULT_STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(cursor.to_dict(), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# 高层写入器
# ---------------------------------------------------------------------------
class TextWriter:
    """维护跨次运行的书写游标，并负责把文本写到 PLC。"""

    def __init__(self, cfg: TextConfig | None = None, state_path=None, reset: bool = False):
        self.cfg = cfg or TextConfig()
        self.cfg.validate()
        self.state_path = Path(state_path) if state_path is not None else DEFAULT_STATE_PATH
        self.cursor = default_cursor(self.cfg) if reset else load_cursor(self.state_path, self.cfg)

    def plan(self, text: str):
        """只排版，不发 TCP；返回 (points, new_cursor)。"""
        return layout_text(text, self.cfg, self.cursor)

    def reset(self) -> Cursor:
        self.cursor = default_cursor(self.cfg)
        return self.cursor

    def write(self, client, text: str, home: bool = True, next_line: bool = False):
        """排版、下发、保存游标，并在最后执行 HOME。

        next_line=False（默认）：游标停在本次文字的**行尾**，下一次运行接着
            同一行继续写（右侧放不下时会自动换行到左边界）。
        next_line=True：游标落到**下一行行首**，下一次运行从新的一行左边缘开始。
        """
        points, new_cursor = self.plan(text)

        # 运动参数（速度/加减速）通过 SET_PARAM(6×f32) 下发；
        # 抬笔/落笔高度不需要下发，z 已经编在下面的每个坐标里。
        if points:
            client.set_param(
                self.cfg.vel,
                self.cfg.acc,
                self.cfg.dec,
                self.cfg.jerk,
                self.cfg.z_up,
                self.cfg.z_down,
            )
            client.draw_waypoints(points)

        # 默认停在行尾，下一次接着写；next_line=True 时才落到下一行行首。
        next_cursor = (
            Cursor(self.cfg.origin_x_mm, new_cursor.y_mm - self.cfg.line_height_mm)
            if next_line
            else new_cursor
        )
        self.cursor = next_cursor
        save_cursor(self.state_path, next_cursor)

        if home:
            client.home()

        return next_cursor