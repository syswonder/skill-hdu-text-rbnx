# skill-write_text-rbnx

Robonix skill that converts Chinese/English text into pen-up/down
waypoints and executes them through the plotter primitive.

The skill never talks TCP or PLC. It resolves
`robonix/primitive/plotter/{trajectory,home,geometry}` through Atlas and
sends absolute millimetre waypoints. Homing and the global motion profile
remain primitive tools (`plotter_home`, `plotter_set_params`); this skill
never sends `SET_PARAM`.

## Capability surface

| Contract | Mode | Transport | Source / handler |
| --- | --- | --- | --- |
| `robonix/skill/write_text/driver` | rpc | gRPC | `Driver(CMD_INIT, config_json)` — lifecycle gate |
| `robonix/skill/write_text/write` | rpc + MCP | MCP | `write_text_write(text, font_size_mm, line_spacing, letter_spacing_mm, origin_x, origin_y, dry_run, auto_home, workspace)` |

The MCP tool returns `success`, `message`, `points`, the planned bounding
box (`min_x`, `max_x`, `min_y`, `max_y`), and `elapsed_s`. Every call
validates the complete plan before moving. Out-of-workspace points, an
empty plan, or more than `max_points` waypoints return `success=false` with
no motion.

`dry_run=true` plans and validates only; `auto_home=true` runs the
primitive home routine after a successful write. Numeric request fields use
`0` as "use the configured default" (`origin_y <= 0` selects the configured
value, where a negative config means top alignment).

## Text pipeline

```text
text
  ├─ ASCII A-Z a-z 0-9 ──→ geometry/hershey_simplex.py (single-stroke vector font)
  └─ 3755 GB2312 level-1 hanzi ─→ geometry/hanzi_data.py (stroke medians)
                │
                ▼
geometry/text.py   scale (cap height = font_size_mm), letter spacing, line
                   spacing, cursor, overflow checks
                │
                ▼
write_text/text.py   Workspace / TextOptions → geometry TextConfig → absolute (x, y, z)
                │
                ▼
write_text/plan.py   dedupe → workspace / pen-height checks → max_points → bounds
                │
                ▼
write_text/primitive_client.py   Trajectory → plotter primitive
```

The vendored single-stroke font contains **no punctuation**: `，。！？：；、`
and their ASCII equivalents raise `TextError` instead of being silently
dropped. ASCII letters (both cases), digits, spaces, and newlines are
supported. There is no left/centre/right alignment; horizontal placement is
`origin_x`.

## Boot ordering

This skill must come **after** `primitive-hdu-cartesian-plotter-rbnx` in the deployment
manifest. Activation resolves `trajectory`, `home`, and `geometry`; if the
primitive is not registered yet, activation returns `Deferred` and retries on
the next MCP call.

## Driver-init lifecycle

`scripts/start.sh` execs `python3 -m write_text.main`.

1. `CMD_INIT`: parse and validate config only; no primitive connection.
2. `CMD_ACTIVATE`: resolve the three primitive contracts through Atlas, then
   read `geometry` once to refresh the workspace.
3. `CMD_DEACTIVATE` / `CMD_SHUTDOWN`: close the gRPC channels.

## Layout

```text
skill-write_text-rbnx/
├── package_manifest.yaml
├── capabilities/
│   ├── driver.v1.toml                   # robonix/skill/write_text/driver
│   ├── write.v1.toml                    # robonix/skill/write_text/write
│   ├── lib/plot/{msg,srv}/...           # Workspace.msg + WriteText.srv
│   ├── lib/plotter/{msg,srv}/...        # mirrored primitive IDL (consume-only)
│   └── primitive/plotter/...            # mirrored, consume-only contracts
├── write_text/
│   ├── main.py                          # MCP tool + lifecycle
│   ├── text.py                          # Workspace/TextOptions → geometry → waypoints
│   ├── plan.py                          # dedupe, validation, bounds
│   ├── primitive_client.py              # Atlas discovery + gRPC calls
│   └── geometry/                        # vendored font + layout (text, hershey_simplex, hanzi_data)
├── scripts/{build,start}.sh
├── tests/{test_text,test_plan}.py
├── config.spec
└── CAPABILITY.md
```

The mirrored `capabilities/primitive/plotter/*` files exist only so
`rbnx codegen --mcp` emits the gRPC stubs this skill calls. They are not
listed in `package_manifest.yaml`, so Atlas does not treat the skill as a
provider of those contracts.

## Config

Deployment config lives in the `write_text` skill `config` block of
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
origin_y: -1.0         # negative -> default: block top-aligned at workspace top
auto_home: true        # as shipped in robonix_manifest.yaml
resolve_timeout_s: 60.0
max_points: 50000
trajectory_timeout_s: 600.0
```

Per-call MCP fields override the config. Numeric fields use `0` as the
"use configured default" sentinel; a negative configured `origin_y` selects
the default top-aligned layout.

Speed, acceleration, deceleration, and jerk are **not** skill config. They
are global PLC parameters; set them once with `plotter_set_params` and they
survive every write.

## Coordinates and pen heights

- Units: millimetres; paper origin at the lower-left, +X right, +Y up.
- The drawable area (`x_max`/`y_max`) and pen heights (`z_up`/`z_down`) are
  read from the plotter primitive's `geometry` contract on activation
  (`get_geometry()`); this skill hardcodes none of them. If the lookup fails,
  activation degrades to the configured fallback workspace.
- The skill encodes `z_up` / `z_down` into every waypoint; the PLC uses the
  per-point `z`.

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
rbnx caps | grep skill/write_text
# Expected: write_text provider with
#   robonix/skill/write_text/{driver,write}
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
- Power loss discards the absolute position; home manually before the next job.

## License

MIT. See [LICENSE](../../../LICENSE).