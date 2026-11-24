#!/usr/bin/env bash
# Build phase: generate gRPC stubs for the consumed plotter primitive and
# MCP dataclasses for this skill's public text tool.
#
# The package is pure Python. The only build-time concern is rbnx codegen:
#   * plotter_pb2 / robonix_contracts_pb2_grpc   (primitive client)
#   * plot_mcp                                    (WriteText)
#   * lifecycle_pb2 and atlas codegen             (Skill.run runtime)
set -euo pipefail
PKG="${RBNX_PACKAGE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
ROOT="$(cd "$PKG/../../.." && pwd)"
cd "$PKG"

CLEAN="${RBNX_BUILD_CLEAN:-}"
if [[ "$CLEAN" == "1" ]]; then
    echo "[xyz_text/build] clean: removing rbnx-build/"
    rm -rf rbnx-build
fi
mkdir -p rbnx-build

# Deployment-wide uv environment (shared by the primitive and both skills).
bash "$ROOT/scripts/uv_sync.sh"

# Generate with the deployment venv so the gencode matches the grpcio/protobuf
# pins that start.sh runs with (see pyproject.toml).
export RBNX_CODEGEN_PYTHON="$ROOT/.venv/bin/python"

FLAGS=(--out-dir "$PKG/rbnx-build/codegen" --mcp)
[[ "$CLEAN" == "1" ]] && FLAGS+=(--clean)

echo "[xyz_text/build] rbnx codegen ${FLAGS[*]}"
rbnx codegen -p "$PKG" "${FLAGS[@]}"

touch "$PKG/rbnx-build/.rbnx-built"
echo "[xyz_text/build] done."