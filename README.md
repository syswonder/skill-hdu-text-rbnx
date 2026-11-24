# skill-text-rbnx

Robonix **skill** that converts Chinese/English text into pen-up/down
waypoints and executes them through the plotter primitive. It owns
`robonix/skill/text/*`. Drawing, prompt-to-image, and image tracing live in
[`skill-draw-rbnx`](../skill-draw-rbnx).

Catalog name: `robonix.skill.write_text`.

The skill never talks TCP or PLC. It resolves
`robonix/primitive/plotter/{trajectory,home}` through Atlas and sends
absolute millimetre waypoints. Homing and the global motion profile remain
primitive tools (`plotter_home`, `plotter_set_params`); this skill never sends
`SET_PARAM`.

## Capability surface

| Contract | Mode | Transport | Source / handler |
| --- | --- | --- | --- |
| `robonix/skill/text/driver` | rpc | gRPC | `Driver(CMD_INIT, config_json)` — lifecycle gate |
| `robonix/skill/text/write_text` | rpc + MCP | MCP | `WriteText(text, font_size_mm, line_spacing, letter_spacing_mm, origin_x, origin_y, dry_run, auto_home, workspace)` |

Every call validates the complete plan before moving. Out-of-workspace
points, an empty plan, or more than `max_points` waypoints return
`success=false` with no motion.

## Text pipeline

```text
text
  ├─ ASCII A-Z a-z 0-9 ──→ SDK hershey_simplex.py (single-stroke vector font)
  └─ 3755 GB2312 level-1 hanzi ─→ SDK hanzi_data.py (stroke centrelines)
                │
                ▼
SDK text.py     scale (cap height = font_size_mm), letter spacing, line
                spacing, wrapping, overflow checks, cursor
                │
                ▼
xyz_text/text.py   map to Workspace / TextOptions → absolute (x, y, z)
                │
                ▼
plan.py         dedupe → workspace / pen-height checks → max_points → bounds
                │
                ▼
primitive_client.py  Trajectory → plotter primitive
```

The SDK single-stroke font contains **no punctuation**: `，。！？：；、` and
their ASCII equivalents return `TextError` instead of being silently dropped.
ASCII letters (both cases), digits, spaces, and newlines are supported. There
is no left/centre/right alignment; horizontal placement is `origin_x`.

## Boot ordering

This skill must come **after** `primitive-hdu-cartesian-plotter-rbnx` in the deployment
manifest. Activation resolves `trajectory` and `home`; if the primitive is not
registered yet, activation returns `Deferred` and retries on the next MCP
call.

## Driver-init lifecycle

`scripts/start.sh` execs `python3 -m xyz_text.main`.

1. `CMD_INIT`: parse and validate config only; no primitive connection.
2. `CMD_ACTIVATE`: resolve the two primitive contracts through Atlas.
3. `CMD_DEACTIVATE` / `CMD_SHUTDOWN`: close the gRPC channels.

## Layout

```text
skill-text-rbnx/
├── package_manifest.yaml
├── capabilities/
│   ├── text_driver.v1.toml
│   ├── write_text.v1.toml
│   ├── lib/plot/{msg,srv}/...           # WriteText + Workspace IDL
│   └── primitive/plotter/...            # mirrored, consume-only contracts
├── xyz_text/
│   ├── main.py                          # MCP tools + lifecycle
│   ├── text.py                          # Workspace/TextOptions → waypoints
│   ├── plan.py                          # dedupe, validation, bounds
│   ├── primitive_client.py              # Atlas discovery + gRPC calls
│   └── geometry/                        # vendored font + layout (text, hershey_simplex, hanzi_data)
├── scripts/{build,start}.sh
├── tests/{test_text,test_plan}.py
├── config.spec
└── CAPABILITY.md
```

The mirrored `capabilities/primitive/plotter/*` files exist only so
`rbnx codegen --mcp` emits the gRPC stubs this skill calls. They are **not**
listed in `package_manifest.yaml`, so Atlas does not treat the skill as a
provider of those contracts.

## Config

Deployment config lives in the `xyz_text` skill `config` block of
[`robonix_manifest.yaml`](../../../robonix_manifest.yaml). Full defaults and
constraints are in [`config.spec`](config.spec).

```yaml
# Drawable area and pen heights are NOT configured here. They are read
# from the primitive's `geometry` contract on activation, so this skill
# carries no machine-specific numbers and works on any sized rig.
font_size_mm: 10.0     # cap height
line_spacing: 1.5      # baseline-to-baseline = font_size_mm * line_spacing
letter_spacing_mm: 0.5
origin_x: 5.0          # left edge of the text origin
origin_y: -1.0         # negative -> SDK default: block top-aligned at workspace top
auto_home: false       # home after a successful write
resolve_timeout_s: 60.0
max_points: 50000
trajectory_timeout_s: 600.0
```

Per-call MCP fields override the config. Numeric fields use `0` as the
"use configured default" sentinel; a negative configured `origin_y` selects
the SDK default top-aligned layout.

Speed, acceleration, deceleration, and jerk are **not** skill config. They
are global PLC parameters; set them once with `plotter_set_params` and they
survive every write.

## Coordinates and pen heights

- Units: millimetres; paper origin at the lower-left, +X right, +Y up.
- The drawable area (`x_max`/`y_max`) and pen heights (`z_up`/`z_down`) are
  reported by the plotter primitive's `PlotterStatus` on activation; this
  skill hardcodes none of them.
- The skill encodes `z_up` / `z_down` into every waypoint; the PLC uses the
  per-point `z`.

The URDF describes the body only and is not used for control.

## Build / run standalone

The deployment shares one uv environment at the repository root; this package
does not carry its own `requirements.txt`.

```bash
bash scripts/build.sh                        # uv sync + rbnx codegen
uv run --project ../../.. pytest tests -q
```

`rbnx boot` launches the skill from the manifest. To run it manually, use the
same Atlas endpoint as the primitive:

```bash
ROBONIX_ATLAS=127.0.0.1:50051 bash scripts/start.sh
```

Tests cover the pure geometry and config layers; they need neither the
Robonix runtime nor a PLC.

## Verification

```bash
rbnx caps | grep skill/text
# Expected: xyz_text provider with
#   robonix/skill/text/{driver,write_text}
```

Then use a dry run before moving:

```text
Ask Pilot: write "Hello XYZ" with dry_run=true and report the point count.
```

An empty plan or an out-of-bounds plan returns `success=false` before any
motion. Unsupported punctuation fails the same way.

## Limits

- No punctuation and no hanzi outside the 3755 level-1 set.
- No alignment modes; position with `origin_x` / `origin_y`.
- No per-point speed or pressure control.
- No STOP command; a failed job leaves the pen where it stopped.
- Power loss discards the absolute position; home before the next job.

## License

MIT. See [LICENSE](../../../LICENSE).