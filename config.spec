# Runtime config accepted by the XYZ text skill.
#
# This file documents the mapping passed as this package's `config:` value in
# robonix_manifest.yaml. Missing keys use the defaults below. Per-call MCP
# fields override these values; fields left at 0 / negative fall back to
# config as documented in each .srv.
#
# Everything is millimetres, matching the SDK and the PLC.

config:
  # Workspace geometry (drawable area + pen heights) is authoritative in the
  # plotter primitive and is read from its `geometry` contract on activation.
  # It is NOT configured here; a `workspace:` mapping in config is only a
  # fallback used when that contract is unreachable (the skill logs an error).

  # ── write_text defaults ────────────────────────────────────────────
  # float mm > 0, default: 10.0. Cap height of the text.
  font_size_mm: 10.0

  # float multiplier >= 1.0, default: 1.5. Baseline-to-baseline distance
  # = font_size_mm * line_spacing.
  line_spacing: 1.5

  # float mm >= 0, default: 0.5. Extra gap after every glyph.
  letter_spacing_mm: 0.5

  # float mm, default: 5.0. Left edge of the text origin.
  origin_x: 5.0

  # float mm, default: -1.0 (sentinel). Negative means "SDK default":
  # the top edge of the text block sits at the workspace top and lines
  # flow downward. A value >= 0 is the baseline of the first line.
  origin_y: -1.0

  # ── motion profile ─────────────────────────────────────────────────
  # Speed, acceleration, deceleration, and jerk are NOT skill config:
  # they are global PLC parameters owned by the plotter primitive. Set
  # them with the primitive's plotter_set_params MCP tool (or
  # robonix/primitive/plotter/set_params) before writing. This skill only
  # sends waypoints, so a user-selected profile survives every job.
  #
  # Pen heights below (workspace.z_up/z_down) are planning geometry: the
  # skill encodes them into each waypoint's z, because the PLC uses the
  # per-point z rather than its own configured pen heights.

  # bool, default: false. If true, run the primitive home routine after
  # every successful write_text. The per-call auto_home flag ORs with
  # this.
  auto_home: false

  # ── limits and timeouts ────────────────────────────────────────────
  # float seconds, default: 60.0. How long activation waits for the
  # primitive contracts to appear in Atlas.
  resolve_timeout_s: 60.0

  # int, default: 50000. Reject a plan larger than this before moving.
  max_points: 50000

  # float seconds, default: 600.0. Budget for the whole trajectory and
  # homing gRPC calls. The primitive estimates per-point timeouts on top
  # of this; a single job is still bounded by the PLC ACK.
  trajectory_timeout_s: 600.0