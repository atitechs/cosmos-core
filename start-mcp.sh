#!/usr/bin/env bash
# AI-Bran MCP Server launcher — used by Claude Code via .mcp.json
# Ensures venv + correct cwd + sane PYTHONPATH regardless of caller.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif command -v python3.11 >/dev/null 2>&1; then
  PY="python3.11"
else
  PY="python3"
fi

export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH:-}"
exec "$PY" -m core.api.mcp_server
