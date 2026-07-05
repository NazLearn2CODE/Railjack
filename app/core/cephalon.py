import os
import json
from pathlib import Path
from functools import lru_cache

@lru_cache(maxsize=1)
def probe(root: Path) -> dict:
    """Check workspace root for compliance with the Cephalon protocol.

    Checks:
    - CLAUDE.md (instructions)
    - CodeCompass.md (navigation map)
    - A-project/index.md (project index)
    - Obsidian MCP (active server in .mcp.json or ORBITER_MCP_SERVERS)
    """
    root = Path(root).resolve()
    
    # Check Obsidian MCP in ORBITER_MCP_SERVERS
    obsidian_mcp = False
    mcp_env = os.environ.get("ORBITER_MCP_SERVERS", "").strip()
    if mcp_env:
        try:
            parsed = json.loads(mcp_env)
            if isinstance(parsed, dict) and "obsidian" in parsed:
                obsidian_mcp = True
        except Exception:
            # Fallback to substring match if JSON decode fails
            if "obsidian" in mcp_env.lower():
                obsidian_mcp = True

    # Check Obsidian MCP in .mcp.json
    if not obsidian_mcp:
        mcp_json_path = root / ".mcp.json"
        if mcp_json_path.is_file():
            try:
                data = json.loads(mcp_json_path.read_text(encoding="utf-8"))
                servers = data.get("mcpServers", {}) or data.get("servers", {})
                if "obsidian" in servers or any("obsidian" in str(k).lower() for k in servers.keys()):
                    obsidian_mcp = True
            except Exception:
                pass

    checks = {
        "claude_md": (root / "CLAUDE.md").is_file(),
        "code_compass": (root / "CodeCompass.md").is_file(),
        "project_index": (root / "A-project" / "index.md").is_file(),
        "obsidian_mcp": obsidian_mcp,
    }

    true_count = sum(1 for v in checks.values() if v)
    if true_count == len(checks):
        level = "full"
    elif true_count > 0:
        level = "partial"
    else:
        level = "none"

    return {
        "root": str(root),
        "level": level,
        "checks": checks,
    }
