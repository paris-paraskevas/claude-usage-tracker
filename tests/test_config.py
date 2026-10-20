"""config.json: typed settings, validation, migration from <= 0.3, unknown keys ignored."""
import json

from ctu import config, paths


def test_validate_patch():
    clean, errors = config.validate_patch({"overlay": {"opacity": 80, "fields": ["5h", "ctx"]},
                                           "team_tz": "UTC", "status_components": ["API"]})
    assert not errors and clean["overlay"] == {"opacity": 80, "fields": ["5h", "ctx"]}
    _, errors = config.validate_patch({"overlay": {"opacity": True}})          # bool is not an int
    assert "overlay.opacity" in errors
    _, errors = config.validate_patch(["not", "an", "object"])
    assert errors
    clean, _ = config.validate_patch({"remote_relay_url": "https://relay.example.dev/"})
    assert clean["remote_relay_url"] == "https://relay.example.dev"


def test_merge_is_one_level_deep():
    cfg = config.merge(config.DEFAULTS, {"alerts": {"incident": True}})
    assert cfg["alerts"]["incident"] is True and cfg["alerts"]["will_hit"] is True
    assert config.DEFAULTS["alerts"]["incident"] is False                    # defaults untouched (opt-in)


def test_load_migrates_legacy_and_ignores_unknown(data_dir):
    paths.DATA_DIR.mkdir(parents=True)
    paths.CONFIG_PATH.write_text(json.dumps({
        "notify_session_waiting": True, "show_widget_on_start": False, "show_bar_on_start": False,
        "predictive_alerts": False, "widget_width": 392, "bar_fields": ["dir"], "danger_alerts": True,
        "dashboard_port": 8790, "team_tz": "Europe/Athens", "status_components": ["API", 7],
        "overlay": {"opacity": 60, "future_key": 1}, "something_new": {"x": 1}}), encoding="utf-8")
    cfg = config.load()
    assert cfg["session_hook"] is True and cfg["overlay"]["shown"] is False
    assert cfg["alerts"]["will_hit"] is False and cfg["alerts"]["limited"] is True
    assert cfg["dashboard_port"] == 8790 and cfg["status_components"] == ["API"]
    assert cfg["overlay"]["opacity"] == 60 and "future_key" not in cfg["overlay"]
    assert "widget_width" not in cfg and "something_new" not in cfg
    config.save(cfg)
    saved = json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
    assert set(saved) == set(config.DEFAULTS)                                # only known keys written back


def test_load_creates_defaults_and_survives_garbage(data_dir):
    assert config.load()["overlay"]["shown"] is True and paths.CONFIG_PATH.exists()
    paths.CONFIG_PATH.write_text("{nope", encoding="utf-8")
    assert config.load()["alerts"]["will_hit"] is True
    assert paths.CONFIG_PATH.read_text(encoding="utf-8") == "{nope"         # a broken file isn't overwritten


def test_settings_view_covers_every_setting():
    v = config.settings_view(config.DEFAULTS, version="0.4.0")
    assert set(config.SETTINGS) == set(config.SCHEMA)
    covered = set(config.API_DIRECT) | set(config.API_MAP.values())
    assert covered == set(config.SETTINGS)                                   # every setting is reachable
    assert v["remote"]["relay_url"] == config.DEFAULT_RELAY and v["data"]["refresh_s"] == 300


def test_api_patch_maps_groups_onto_config_keys():
    clean, acts, errors = config.api_patch({"remote": {"enabled": True}, "team": {"tz": "UTC"},
                                            "data": {"refresh_s": 60, "statusline_installed": True},
                                            "about": {"version": "x"}, "status_components_available": ["a"]})
    assert not errors and acts == {"statusline_installed": True}
    assert clean == {"remote_enabled": True, "team_tz": "UTC", "api_fallback_interval_seconds": 60}
