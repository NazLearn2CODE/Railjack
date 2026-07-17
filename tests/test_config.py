"""Config selection (``app/config.py:select_config``).

Uses tmp_path YAMLs + monkeypatches ``CONFIG_DIR`` so the real
``config/*.yaml`` never influence the outcome.
"""

import pytest

from app import config


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
