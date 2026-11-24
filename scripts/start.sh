#!/usr/bin/env bash
# Start phase: exec the native Python text skill process.
set -euo pipefail
PKG="${RBNX_PACKAGE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
ROOT="$(cd "$PKG/../../.." && pwd)"
PY="$ROOT/.venv/bin/python"
cd "$PKG"

if [[ ! -x "$PY" ]]; then
    echo "[xyz_text/start] ERR: missing $PY — run rbnx build" >&2
    exit 2
fi

CODEGEN_PROTO="$PKG/rbnx-build/codegen/proto_gen"
CODEGEN_MCP="$PKG/rbnx-build/codegen/robonix_mcp_types"
if [[ ! -d "$CODEGEN_PROTO" || ! -d "$CODEGEN_MCP" ]]; then
    echo "[xyz_text/start] ERR: codegen output missing — run scripts/build.sh" >&2
    exit 2
fi

export PYTHONPATH="$CODEGEN_PROTO:$CODEGEN_MCP:$PKG:${PYTHONPATH:-}"
if ROBONIX_API="$(rbnx path robonix-api 2>/dev/null)"; then
    export PYTHONPATH="$ROBONIX_API:$PYTHONPATH"
fi

exec "$PY" -u -m xyz_text.main