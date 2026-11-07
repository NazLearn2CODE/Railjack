"""Config selection (``app/config.py:select_config``).

Uses tmp_path YAMLs + monkeypatches ``CONFIG_DIR`` so the real
``config/*.yaml`` never influence the outcome.
"""

import pytest

from app.platform import config


def _write_cfg(path, machine, hostnames):
    path.write_text(
        f"machine: {machine}\nhostnames: {hostnames}\nmodules: []\n"
    )


def test_env_override_wins(monkeypatch, tmp_path):
    # hostname would match alpha, but env should take precedence (checked first)
    _write_cfg(tmp_path / "alpha.yaml", "alpha", "[alpha-host]")
    _write_cfg(tmp_path / "beta.yaml", "beta", "[beta-host]")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setenv("RAILJACK_CONFIG", "beta")
    assert config.select_config().machine == "beta"


def test_hostname_match(monkeypatch, tmp_path):
    _write_cfg(tmp_path / "gamma.yaml", "gamma", "[bazzite, other]")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.delenv("RAILJACK_CONFIG", raising=False)
    # select_config splits on '.' and lowercases, so a FQDN still matches
    monkeypatch.setattr(config.socket, "gethostname", lambda: "Bazzite.Example.Com")
    assert config.select_config().machine == "gamma"


def test_no_env_no_match_lists_available(monkeypatch, tmp_path):
    _write_cfg(tmp_path / "alpha.yaml", "alpha", "[alpha-host]")
    _write_cfg(tmp_path / "beta.yaml", "beta", "[beta-host]")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.delenv("RAILJACK_CONFIG", raising=False)
    monkeypatch.setattr(config.socket, "gethostname", lambda: "nomatch")
    with pytest.raises(RuntimeError) as exc:
        config.select_config()
    msg = str(exc.value)
    assert "alpha" in msg and "beta" in msg  # lists available configs


def test_env_override_no_match_lists_available(monkeypatch, tmp_path):
    _write_cfg(tmp_path / "alpha.yaml", "alpha", "[alpha-host]")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setenv("RAILJACK_CONFIG", "ghost")
    with pytest.raises(RuntimeError) as exc:
        config.select_config()
    assert "alpha" in str(exc.value)


def test_no_configs_at_all(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.delenv("RAILJACK_CONFIG", raising=False)
    with pytest.raises(RuntimeError):
        config.select_config()


def test_dock_optional_defaults_none():
    # No `dock:` key → the field is None, so the frontend renders no dock.
    cfg = config.MachineConfig(machine="x", hostnames=["x"], modules=[])
    assert cfg.dock is None


def test_dock_parses_from_yaml(monkeypatch, tmp_path):
    (tmp_path / "x.yaml").write_text(
        "machine: x\nhostnames: [x]\nmodules: []\n"
        "dock:\n  title: LIVE\n  url: http://localhost:7681\n  height: 220\n"
    )
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.delenv("RAILJACK_CONFIG", raising=False)
    monkeypatch.setattr(config.socket, "gethostname", lambda: "x")
    cfg = config.select_config()
    assert cfg.dock is not None
    assert cfg.dock.title == "LIVE"
    assert cfg.dock.url == "http://localhost:7681"
    assert cfg.dock.height == 220


def test_dsh_launch_url_attaches_and_caches_token(monkeypatch):
    from types import SimpleNamespace
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "_DSH_TOKEN_CACHE", {})
    calls: list[list[str]] = []

    def fake_run(argv, **_kw):
        calls.append(argv)
        if argv[0] == "systemctl":
            return SimpleNamespace(returncode=0, stdout="abc123inv\n")
        return SimpleNamespace(
            returncode=0,
            stdout="dsh web: http://127.0.0.1:3080/?token=tok_first\ndsh web: http://127.0.0.1:3080/?token=tok_latest\n",
        )

    monkeypatch.setattr(main_mod.subprocess, "run", fake_run)
    m = config.Module(
        id="dsh",
        title="DSH",
        kind="iframe",
        url="http://127.0.0.1:3080",
        manage=config.ManageSpec(type="systemd-user", unit="dsh-web.service"),
    )
    first = main_mod._sanitize(m)["url"]
    second = main_mod._sanitize(m)["url"]
    assert first == "http://127.0.0.1:3080/?token=tok_latest"
    assert second == first
    # Second call hits InvocationID cache — journalctl ran only once.
    assert [c[0] for c in calls] == ["systemctl", "journalctl", "systemctl"]


def test_dsh_launch_url_degrades_cleanly_on_error(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "_DSH_TOKEN_CACHE", {})
    monkeypatch.setattr(
        main_mod.subprocess,
        "run",
        lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError("no systemctl")),
    )
    assert main_mod._dsh_launch_url("http://127.0.0.1:3080", "dsh-web.service") == "http://127.0.0.1:3080"


