from __future__ import annotations

from pathlib import Path

import pytest

from headroom.config import Config, ConfigError, config_from_mapping, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_examples_match_defaults() -> None:
    toml_config, toml_path = load_config(ROOT / "config.example.toml")
    yaml_config, yaml_path = load_config(ROOT / "config.example.yaml")
    assert toml_path is not None and yaml_path is not None
    assert toml_config == Config()
    assert yaml_config == Config()


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml")


def test_no_config_uses_defaults(tmp_path: Path) -> None:
    config, path = load_config(env={}, home=tmp_path)
    assert config == Config()
    assert path is None


def test_appdata_config_is_found(tmp_path: Path) -> None:
    appdata = tmp_path / "Roaming"
    folder = appdata / "Headroom"
    folder.mkdir(parents=True)
    (folder / "config.toml").write_text(
        "[thresholds]\nram_percent = 90\n",
        encoding="utf-8",
    )
    config, path = load_config(env={"APPDATA": str(appdata)}, home=tmp_path)
    assert path == folder / "config.toml"
    assert config.ram_percent == 90


def test_yaml_and_toml_non_defaults_match(tmp_path: Path) -> None:
    body_toml = """
[monitor]
interval_seconds = 60

[thresholds]
ram_percent = 90
ram_sustained_seconds = 30
disk_warn_free_gb = 40
disk_critical_free_gb = 10
drives = ["D:", "e:/"]

[report]
top_processes = 5
scan_top = 3

[notifications]
toast = false

[cleanup]
enabled = ["user_temp", "chrome_cache"]

[logging]
path = "~/headroom.log"
"""
    body_yaml = """
monitor:
  interval_seconds: 60
thresholds:
  ram_percent: 90
  ram_sustained_seconds: 30
  disk_warn_free_gb: 40
  disk_critical_free_gb: 10
  drives: ["D:", "e:/"]
report:
  top_processes: 5
  scan_top: 3
notifications:
  toast: false
cleanup:
  enabled: ["user_temp", "chrome_cache"]
logging:
  path: "~/headroom.log"
"""
    toml_path = tmp_path / "headroom.toml"
    yaml_path = tmp_path / "headroom.yaml"
    toml_path.write_text(body_toml, encoding="utf-8")
    yaml_path.write_text(body_yaml, encoding="utf-8")
    assert load_config(toml_path)[0] == load_config(yaml_path)[0]
    loaded = load_config(toml_path)[0]
    assert loaded.interval_seconds == 60
    assert loaded.drives == ("D:", "E:")
    assert loaded.toast is False
    assert loaded.enabled_cleanup == ("user_temp", "chrome_cache")
    assert loaded.disk_warn_free_bytes == 40 * 1024**3
    assert loaded.log_path == Path.home() / "headroom.log"


def test_unknown_cleanup_path_is_rejected() -> None:
    with pytest.raises(ConfigError, match="paths"):
        config_from_mapping({"cleanup": {"paths": ["C:/Users/me/Documents"]}})


def test_unknown_cleanup_id_is_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown cleanup target"):
        config_from_mapping({"cleanup": {"enabled": ["C:/Users/me/Documents"]}})


def test_critical_must_be_below_warn() -> None:
    with pytest.raises(ConfigError, match="disk_critical_free_gb"):
        config_from_mapping(
            {
                "thresholds": {
                    "disk_warn_free_gb": 10,
                    "disk_critical_free_gb": 30,
                }
            }
        )


def test_bool_is_not_a_number() -> None:
    with pytest.raises(ConfigError, match="ram_percent"):
        config_from_mapping({"thresholds": {"ram_percent": True}})


def test_interval_minimum() -> None:
    with pytest.raises(ConfigError, match="interval_seconds"):
        config_from_mapping({"monitor": {"interval_seconds": 1}})


def test_yaml_tags_are_not_executed(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    config = tmp_path / "bad.yaml"
    config.write_text(
        "cleanup: !!python/object/apply:os.system ['touch %s']\n" % marker,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config)
    assert not marker.exists()


def test_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text("this = [\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="could not read"):
        load_config(path)


def test_empty_file_is_defaults(tmp_path: Path) -> None:
    path = tmp_path / "empty.toml"
    path.write_text("\n", encoding="utf-8")
    assert load_config(path)[0] == Config()
