import json
from pathlib import Path

def parse_skill_md(file_path: Path) -> dict | None:
    """Parse YAML-like frontmatter from a SKILL.md file."""
    try:
        content = file_path.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        frontmatter_text = parts[1]
        
        meta = {}
        current_key = None
        current_value = []
        
        for line in frontmatter_text.splitlines():
            if not line:
                continue
            if ":" in line and not line.startswith(" "):
                if current_key:
                    meta[current_key] = "\n".join(current_value).strip()
                k, v = line.split(":", 1)
                current_key = k.strip()
                v_clean = v.strip()
                if v_clean in (">", ">-", "|", "|-"):
                    current_value = []
                else:
                    current_value = [v_clean]
            else:
                if current_key:
                    current_value.append(line.rstrip())
                    
        if current_key:
            meta[current_key] = "\n".join(current_value).strip()
            
        return meta
    except Exception:
        return None


def scan_skills(workspace_root: Path) -> list[dict]:
    """Scan for agent skills in the workspace, user directory, and installed plugins."""
    skills = []
    
    # 1. Workspace skills: <workspace>/.claude/skills/*
    ws_skills_dir = Path(workspace_root) / ".claude" / "skills"
    if ws_skills_dir.is_dir():
        for skill_dir in ws_skills_dir.iterdir():
            if skill_dir.is_dir():
                skill_md = skill_dir / "SKILL.md"
                if skill_md.is_file():
                    meta = parse_skill_md(skill_md)
                    if meta and "name" in meta:
                        skills.append({
                            "name": meta["name"],
                            "description": meta.get("description", ""),
                            "source": "PROJECT",
                            "path": str(skill_dir.resolve())
                        })
                        
    # 2. User skills: ~/.claude/skills/*
    user_skills_dir = Path("~/.claude/skills").expanduser()
    if user_skills_dir.is_dir():
        for skill_dir in user_skills_dir.iterdir():
            if skill_dir.is_dir():
                skill_md = skill_dir / "SKILL.md"
                if skill_md.is_file():
                    meta = parse_skill_md(skill_md)
                    if meta and "name" in meta:
                        skills.append({
                            "name": meta["name"],
                            "description": meta.get("description", ""),
                            "source": "USER",
                            "path": str(skill_dir.resolve())
                        })

    # 3. Plugin skills: ~/.claude/plugins/installed_plugins.json
    plugins_json = Path("~/.claude/plugins/installed_plugins.json").expanduser()
    if plugins_json.is_file():
        try:
            data = json.loads(plugins_json.read_text(encoding="utf-8"))
            plugins = data.get("plugins", {})
            for key, instances in plugins.items():
                if not instances or not isinstance(instances, list):
                    continue
                first_instance = instances[0]
                install_path = first_instance.get("installPath")
                if not install_path:
                    continue
                
                plugin_name = key.split("@")[0]
                plugin_skills_dir = Path(install_path) / "skills"
                if plugin_skills_dir.is_dir():
                    for skill_dir in plugin_skills_dir.iterdir():
                        if skill_dir.is_dir():
                            skill_md = skill_dir / "SKILL.md"
                            if skill_md.is_file():
                                meta = parse_skill_md(skill_md)
                                if meta and "name" in meta:
                                    skills.append({
                                        "name": meta["name"],
                                        "description": meta.get("description", ""),
                                        "source": f"PLUGIN:{plugin_name}",
                                        "path": str(skill_dir.resolve())
                                    })
        except Exception:
            pass
            
    return skills
