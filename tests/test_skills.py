import json
import importlib
from pathlib import Path
from fastapi.testclient import TestClient
from app.core.skills import parse_skill_md, scan_skills

def test_parse_skill_md(tmp_path):
    skill_file = tmp_path / "SKILL.md"
    
    # 1. Standard frontmatter
    skill_file.write_text("""---
name: test-skill
description: Simple description here.
---
# Body content
""", encoding="utf-8")
    
    meta = parse_skill_md(skill_file)
    assert meta == {
        "name": "test-skill",
        "description": "Simple description here."
    }

    # 2. Folded description
    skill_file.write_text("""---
name: run
description: >
  Launch the Orbiter gateway + dashboard.
  Multi line description.
---
# Body content
""", encoding="utf-8")
    
    meta = parse_skill_md(skill_file)
    assert meta["name"] == "run"
    assert "Launch the Orbiter gateway" in meta["description"]
    assert "Multi line description." in meta["description"]


def test_scan_skills(tmp_path, monkeypatch):
    # Setup mock workspace structure
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    # 1. Project skill
    ws_skills_dir = workspace / ".claude" / "skills" / "build"
    ws_skills_dir.mkdir(parents=True)
    (ws_skills_dir / "SKILL.md").write_text("""---
name: build
description: Build the app
---
""", encoding="utf-8")

    # 2. User skill
    user_home = tmp_path / "home"
    user_skills_dir = user_home / ".claude" / "skills" / "test"
    user_skills_dir.mkdir(parents=True)
    (user_skills_dir / "SKILL.md").write_text("""---
name: test
description: Test the app
---
""", encoding="utf-8")

    # 3. Plugin skill
    plugin_dir = tmp_path / "plugins" / "installed" / "my-plugin"
    plugin_skills_dir = plugin_dir / "skills" / "plugin-skill"
    plugin_skills_dir.mkdir(parents=True)
    (plugin_skills_dir / "SKILL.md").write_text("""---
name: plugin-skill
description: A plugin skill
---
""", encoding="utf-8")

    plugins_json = user_home / ".claude" / "plugins" / "installed_plugins.json"
    plugins_json.parent.mkdir(parents=True)
    plugins_json.write_text(json.dumps({
        "plugins": {
            "my-plugin@official": [
                {
                    "installPath": str(plugin_dir)
                }
            ]
        }
    }), encoding="utf-8")

    # Monkeypatch home directory expansion and environment
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setattr(Path, "expanduser", lambda self: Path(str(self).replace("~", str(user_home))))

    # Scan skills
    skills = scan_skills(workspace)
    
    # Validate result
    by_name = {s["name"]: s for s in skills}
    assert "build" in by_name
    assert by_name["build"]["source"] == "PROJECT"
    assert by_name["build"]["path"] == str(ws_skills_dir.resolve())
    
    assert "test" in by_name
    assert by_name["test"]["source"] == "USER"
    
    assert "plugin-skill" in by_name
    assert by_name["plugin-skill"]["source"] == "PLUGIN:my-plugin"


def test_api_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("ORBITER_WORKSPACE_ROOT", str(tmp_path))
    import app.main as main
    importlib.reload(main)

    client = TestClient(main.app)
    r = client.get("/api/skills")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
