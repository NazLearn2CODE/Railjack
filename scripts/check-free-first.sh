#!/usr/bin/env bash
# check-free-first.sh — enforce free-first web research in Railjack module code.
# Fails if app/ uses a disallowed paid / z.ai-MCP search API. Free backends
# (Jina / DuckDuckGo / Brave-free-tier / GNews / agent-reach) are allowed, and
# z.ai GLM for *generation* (api.z.ai/api/anthropic) is allowed — only
# paid SEARCH APIs and the z.ai MCP search/reader are blocked.
# Run:  bash scripts/check-free-first.sh   (wire to pre-commit / .githooks if desired)
set -euo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo "$(dirname "$0")/..")"

# Disallowed patterns in module code (z.ai MCP search + common paid search APIs).
# Provider names as bare substrings catch both URL and SDK-import forms; they are
# distinctive enough that current app/ (Jina/DDG/Brave/GNews) contains none.
patterns='mcp__web_reader|mcp__4_5v|customsearch\.googleapis|serper|tavily'
hits=$(grep -rEil "$patterns" app/ 2>/dev/null || true)

if [[ -n "$hits" ]]; then
  echo "❌ check-free-first: disallowed paid/z.ai-MCP search API in module code:" >&2
  printf '%s\n' "$hits" >&2
  echo "Use the free-first chain (Jina/DDG/Brave/GNews/agent-reach). z.ai GLM for generation is OK." >&2
  exit 1
fi
echo "✓ check-free-first: app/ is free-first (no paid/z.ai-MCP search APIs)."
