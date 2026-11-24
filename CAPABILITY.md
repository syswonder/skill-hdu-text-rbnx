---
description: Text-writing skill — lay out Chinese/English text with the SDK single-stroke font and write it with the plotter pen.
---

# Text skill (`xyz_text`)

Converts text into pen-up/pen-down waypoints using the SDK's single-stroke
font, then executes them through the plotter primitive. It never touches the
PLC directly.

## MCP tool

### `text_write_text`

Write Chinese/English text on the paper.

- `text` (string, required): ASCII `A-Z a-z 0-9`, space, newline, and the 3755
  GB2312 level-1 hanzi in the SDK font.
- **Punctuation is not supported** (`，。！？：；、` etc.); such characters
  return `success=false` rather than being skipped. Strip or replace them
  before calling.
- Layout arguments (millimetres):
  - `font_size_mm` — cap height; default from skill config.
  - `line_spacing` — multiplier of `font_size_mm`.
  - `letter_spacing_mm` — extra gap per glyph.
  - `origin_x` — left edge of the text block.
  - `origin_y` — baseline of the first line; a negative value selects the SDK
    default (block top-aligned at the workspace top).
- `dry_run=true` returns the waypoint count and bounding box without moving.
- `auto_home=true` runs the primitive home routine after a successful write.

Numeric fields use `0` as "use the configured default"; see the tool schema.

## Motion parameters and homing

Speed, acceleration, deceleration, jerk, and the primitive's pen-height
profile are **not** skill config and are never sent by this skill. To change
them, call the primitive tools directly:

- `plotter_set_params` — change the global profile (0 = keep current).
- `plotter_home` — home the machine on request.

Because this skill never calls SET_PARAM, a profile selected by the user
persists across every write.

## Coordinates

- Units: millimetres; paper origin at the lower-left, +X right, +Y up.
- Drawable area and pen heights are reported by the `geometry` contract on
  activation; this skill hardcodes none of them.
- Out-of-bounds plans return `success=false` before any motion.