"""Plex is optional: a run can use Plex, Jellyfin, or both (docs/media-servers-design.md).

Settings validation required PLEX_URL, PLEX_TOKEN, valid_sections and
users_toggle, setup counted as done only with a Plex URL and token, and every
run built and connected a Plex client. Now:

- plex_enabled / jellyfin_enabled switch a server off without deleting its
  settings. Without the flags (every existing settings file), Plex is used
  unless the file has no Plex URL or token and Jellyfin is switched on, so
  existing installs validate and run exactly as before.
- Setup is done once a media server is saved. Jellyfin needs the explicit
  jellyfin_enabled switch, so env vars filled in on a new container don't skip
  the setup wizard.
- Without Plex, the run builds no Plex client, connects to nothing, and leaves
  pins alone (they need Plex until Jellyfin pins exist).
"""

import json
import logging
import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault('fcntl', MagicMock())
for _mod in ('plexapi', 'plexapi.server', 'plexapi.video', 'plexapi.myplex'):
    sys.modules.setdefault(_mod, MagicMock())

from core.config import ConfigManager, jellyfin_configured, plex_enabled, setup_has_media_server  # noqa: E402

PLEX = {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "tok"}
JELLYFIN = {"jellyfin_url": "http://jellyfin:8096", "jellyfin_api_key": "key"}


@pytest.fixture(autouse=True)
def _no_jellyfin_env(monkeypatch):
    for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY"):
        monkeypatch.delenv(key, raising=False)


# -- which servers ----------------------------------------------------------------

@pytest.mark.parametrize("settings, expected", [
    (PLEX, True),                                                     # every existing install
    ({}, True),                                                       # nothing set up: Plex expected, as before
    ({**PLEX, "plex_enabled": False}, False),
    ({"plex_enabled": True}, True),
    ({**JELLYFIN, "jellyfin_enabled": True}, False),                  # Jellyfin-only
    (JELLYFIN, True),                                                 # Jellyfin not switched on explicitly
    ({**PLEX, **JELLYFIN, "jellyfin_enabled": True}, True),           # both
])
def test_plex_enabled(settings, expected):
    assert plex_enabled(settings) is expected


@pytest.mark.parametrize("settings, expected", [
    (JELLYFIN, True),
    ({**JELLYFIN, "jellyfin_enabled": False}, False),
    ({"jellyfin_url": "http://jellyfin:8096"}, False),
    ({"jellyfin_url": "  ", "jellyfin_api_key": "key"}, False),
])
def test_jellyfin_configured(settings, expected):
    assert jellyfin_configured(settings) is expected


def test_jellyfin_env_vars_count(monkeypatch):
    monkeypatch.setenv("JELLYFIN_URL", "http://jellyfin:8096")
    monkeypatch.setenv("JELLYFIN_API_KEY", "key")
    assert jellyfin_configured({})


# -- setup ------------------------------------------------------------------------

@pytest.mark.parametrize("settings, expected", [
    (PLEX, True),
    ({"PLEX_URL": "http://plex:32400"}, False),
    ({}, False),
    ({**PLEX, "plex_enabled": False}, False),
    ({**JELLYFIN, "jellyfin_enabled": True}, True),
    ({**PLEX, "plex_enabled": False, **JELLYFIN, "jellyfin_enabled": True}, True),
    (JELLYFIN, False),
])
def test_setup_has_media_server(settings, expected):
    assert setup_has_media_server(settings) is expected


@pytest.mark.critical
def test_jellyfin_env_vars_alone_never_skip_setup(monkeypatch):
    """The Community Apps template has Jellyfin fields; a new container may start with them filled in."""
    monkeypatch.setenv("JELLYFIN_URL", "http://jellyfin:8096")
    monkeypatch.setenv("JELLYFIN_API_KEY", "key")
    assert not setup_has_media_server({})
    assert setup_has_media_server({"jellyfin_enabled": True})


# -- loading the config -----------------------------------------------------------

def _settings(tmp_path, **changes):
    cache_dir, real_dir = tmp_path / "cache", tmp_path / "media"
    for d in (cache_dir, real_dir):
        d.mkdir(parents=True, exist_ok=True)
    settings = {
        **PLEX,
        "number_episodes": 5, "valid_sections": [1], "days_to_monitor": 99, "users_toggle": False,
        "watchlist_toggle": False, "watchlist_episodes": 3, "watched_move": True,
        "max_concurrent_moves_array": 2, "max_concurrent_moves_cache": 5,
        "cache_dir": str(cache_dir), "real_source": str(real_dir), "plex_source": "/data/",
        "path_mappings": [{"name": "Movies", "plex_path": "/data/movies", "real_path": str(real_dir),
                           "cache_path": str(cache_dir), "enabled": True}],
    }
    for key, value in changes.items():
        if value is None:
            settings.pop(key, None)
        else:
            settings[key] = value
    path = tmp_path / "plexcache_settings.json"
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    config = ConfigManager(str(path))
    config.load_config(persist_updates=False)
    return config


def test_existing_plex_settings_load_as_before(tmp_path):
    config = _settings(tmp_path)
    assert config.plex.enabled and config.plex.plex_url == "http://plex:32400"
    assert not config.jellyfin.enabled


def test_plex_fields_are_still_required_with_plex(tmp_path):
    with pytest.raises(ValueError, match="valid_sections"):
        _settings(tmp_path, valid_sections=None)
    with pytest.raises(ValueError, match="'PLEX_TOKEN' cannot be empty"):
        _settings(tmp_path, PLEX_TOKEN="")


@pytest.mark.critical
def test_jellyfin_only_settings_load(tmp_path):
    config = _settings(tmp_path, PLEX_URL=None, PLEX_TOKEN=None, valid_sections=None, users_toggle=None,
                       jellyfin_enabled=True, **JELLYFIN)
    assert not config.plex.enabled and config.plex.plex_url == ""
    assert config.jellyfin.enabled


def test_switching_plex_off_keeps_its_settings(tmp_path):
    config = _settings(tmp_path, plex_enabled=False, **JELLYFIN)
    assert not config.plex.enabled and config.plex.plex_url == "http://plex:32400"


def test_no_media_server_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="No media server is set up"):
        _settings(tmp_path, plex_enabled=False)
    with pytest.raises(ValueError, match="No media server is set up"):
        _settings(tmp_path, plex_enabled=False, jellyfin_enabled=False, **JELLYFIN)


def test_jellyfin_can_be_switched_off(tmp_path):
    assert not _settings(tmp_path, jellyfin_enabled=False, **JELLYFIN).jellyfin.enabled


# -- the run without Plex ---------------------------------------------------------

def _app(plex_on):
    from core.app import PlexCacheApp
    app = object.__new__(PlexCacheApp)
    app.config_manager = MagicMock()
    app.config_manager.plex.enabled = plex_on
    app.config_manager.get_user_tokens_file.return_value = "/tmp/tokens.json"
    app.config_manager.get_rss_cache_file.return_value = "/tmp/rss.xml"
    return app


def test_no_plex_client_without_plex(caplog):
    app = _app(plex_on=False)
    with caplog.at_level(logging.INFO):
        app._init_plex_manager()
    assert app.plex_manager is None
    assert "[PLEX] Not used" in caplog.text
    app._connect_to_plex()  # nothing to connect to, no error


def test_plex_client_built_with_plex(monkeypatch):
    import core.app as app_module
    built = MagicMock()
    monkeypatch.setattr(app_module, "PlexManager", built)
    app = _app(plex_on=True)
    app._init_plex_manager()
    assert app.plex_manager is built.return_value


def test_plex_pins_wait_for_plex(caplog):
    app = _app(plex_on=False)
    app.plex_manager = None
    app.jellyfin_manager = None
    app.pinned_tracker = MagicMock()
    app.pinned_tracker.list_pins.return_value = [{"rating_key": "1", "type": "movie", "title": "Film"}]
    with caplog.at_level(logging.INFO):
        app._process_pinned_media({})
    assert "1 Plex pin(s) not applied: Plex is not in use" in caplog.text
    assert app.pinned_items == set()


def test_a_run_without_plex_asks_only_jellyfin():
    app = _app(plex_on=False)
    app.plex_manager = None
    app.jellyfin_manager = MagicMock()
    assert [s.key for s in app.media_servers] == ["jellyfin"]
