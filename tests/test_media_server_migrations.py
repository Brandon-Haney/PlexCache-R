"""Settings files from before media servers, through every change the branch makes.

Three shapes, as installs have them:

- a v3.3.0 Plex-only file: loads as Plex-only, and a run rewrites it unchanged;
- a v3.3.0 file with #234's Jellyfin keys (jellyfin_url, jellyfin_api_key,
  jellyfin_path_mappings): Jellyfin stays on, and moving
  jellyfin_path_mappings onto the path mappings (the Libraries tab) changes
  where no Jellyfin file goes;
- a Jellyfin-only file as the setup wizard writes it: survives a run's
  rewrite and loads the same again.
"""

import copy
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

for _mod in ("fcntl", "pwd", "grp", "plexapi", "plexapi.server", "plexapi.myplex",
             "apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.background",
             "apscheduler.triggers", "apscheduler.triggers.cron", "apscheduler.triggers.interval"):
    sys.modules.setdefault(_mod, MagicMock())

from core.config import ConfigManager, setup_has_media_server  # noqa: E402
from core.file_operations import MultiPathModifier  # noqa: E402
from core.jellyfin_api import JellyfinManager, jellyfin_path_table  # noqa: E402

# A v3.3.0 install: the keys a real file has, Plex libraries from the wizard
V330_PLEX = {
    "firststart": False, "PLEX_URL": "http://192.168.1.10:32400", "PLEX_TOKEN": "token",
    "plexcache_client_id": "client-id", "valid_sections": [1, 2], "number_episodes": 5,
    "days_to_monitor": 99, "users_toggle": True, "users": [], "watchlist_toggle": True,
    "watchlist_episodes": 3, "watched_move": True, "max_concurrent_moves_array": 2,
    "max_concurrent_moves_cache": 5, "cache_dir": "/mnt/cache/", "exit_if_active_session": False,
    "cache_limit": "250GB", "cache_eviction_mode": "smart", "create_plexcached_backups": True,
    "cache_associated_files": "subtitles", "pinned_preferred_resolution": "highest",
    "path_mappings": [
        {"name": "Movies", "plex_path": "/data/movies/", "real_path": "/mnt/user/media/movies/",
         "cache_path": "/mnt/cache/media/movies/", "host_cache_path": "/mnt/cache/media/movies/",
         "cacheable": True, "enabled": True, "section_id": 1},
        {"name": "TV Shows", "plex_path": "/data/tv/", "real_path": "/mnt/user/media/tv/",
         "cache_path": "/mnt/cache/media/tv/", "host_cache_path": "/mnt/cache/media/tv/",
         "cacheable": True, "enabled": True, "section_id": 2},
        {"name": "Kids", "plex_path": "/data/kids/", "real_path": "/mnt/user/media/kids/",
         "cache_path": None, "host_cache_path": None, "cacheable": False, "enabled": True, "section_id": 3},
    ],
}
# #234 (v3.3.0): Jellyfin as an extra OnDeck source, its paths translated to Plex's
V330_BOTH = {
    **V330_PLEX,
    "jellyfin_url": "http://192.168.1.10:8096", "jellyfin_api_key": "key", "jellyfin_favorites_toggle": True,
    "jellyfin_skip_users": ["guest"],
    "jellyfin_path_mappings": {
        "/media/movies": "/data/movies",      # covers the Movies mapping
        "/media/shows/": "/data/tv/",          # covers TV Shows (trailing slashes as typed)
        "/media/music": "/data/music",         # covers no mapping: stays
    },
}
# What the setup wizard writes for a Jellyfin-only install
WIZARD_JELLYFIN = {
    **{k: v for k, v in V330_PLEX.items() if k not in ("PLEX_URL", "PLEX_TOKEN", "plexcache_client_id",
                                                        "valid_sections", "path_mappings")},
    "plex_enabled": False, "jellyfin_enabled": True, "jellyfin_url": "http://192.168.1.10:8096",
    "jellyfin_api_key": "key", "users_toggle": False, "valid_sections": [], "jellyfin_skip_users": [],
    "path_mappings": [
        {"name": "Movies", "plex_path": "", "real_path": "/mnt/user/movies/", "cache_path": "/mnt/cache/movies/",
         "host_cache_path": "/mnt/cache/movies/", "cacheable": True, "enabled": True,
         "jellyfin_path": "/media/movies", "jellyfin_library_id": "lib-movies", "jellyfin_enabled": True},
        {"name": "Shows", "plex_path": "", "real_path": "/mnt/user/tv/", "cache_path": "/mnt/cache/tv/",
         "host_cache_path": "/mnt/cache/tv/", "cacheable": True, "enabled": True,
         "jellyfin_path": "/media/tv", "jellyfin_library_id": "lib-tv", "jellyfin_enabled": True},
    ],
}
JELLYFIN_FILES = ["/media/movies/Dune (2021)/Dune.mkv", "/media/shows/Andor/Season 2/Andor S02E01.mkv",
                  "/media/music/Album/track.flac", "/media/other/film.mkv"]


@pytest.fixture(autouse=True)
def _no_jellyfin_env(monkeypatch):
    for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_SKIP_USERS"):
        monkeypatch.delenv(key, raising=False)


def _write(tmp_path, settings, name="plexcache_settings.json"):
    path = tmp_path / name
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return path


def _load(path, persist=False):
    config = ConfigManager(str(path))
    config.load_config(persist_updates=persist)
    return config


def _where_jellyfin_files_go(config):
    """Each Jellyfin file's real path, as a run works it out (None: not used)."""
    table, skipped = jellyfin_path_table(config.jellyfin, config.paths.path_mappings)
    jellyfin = JellyfinManager("http://jf", "k", path_mappings=table, skip_prefixes=skipped)
    modifier = MultiPathModifier(config.paths.path_mappings)
    out = {}
    for path in JELLYFIN_FILES:
        run_path = jellyfin.to_plex_path(path)
        out[path] = modifier.convert_plex_to_real(run_path)[0] if run_path else None
    return out


def test_a_plex_only_v330_file_loads_and_rewrites_unchanged(tmp_path):
    path = _write(tmp_path, V330_PLEX)
    config = _load(path, persist=True)
    assert config.plex.enabled and not config.jellyfin.enabled
    assert setup_has_media_server(V330_PLEX)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["path_mappings"] == V330_PLEX["path_mappings"]
    assert not any(key.startswith(("plex_enabled", "jellyfin")) for key in saved)


@pytest.mark.critical
def test_moving_jellyfin_path_mappings_changes_where_no_file_goes(tmp_path):
    from web.services.settings_service import SettingsService
    before = _where_jellyfin_files_go(_load(_write(tmp_path, V330_BOTH)))
    assert before["/media/movies/Dune (2021)/Dune.mkv"] == "/mnt/user/media/movies/Dune (2021)/Dune.mkv"
    assert before["/media/other/film.mkv"] is None

    raw = copy.deepcopy(V330_BOTH)
    svc = SettingsService.__new__(SettingsService)
    svc._load_raw = lambda: V330_BOTH
    with patch("web.services.settings_service.DATA_DIR", tmp_path):
        assert svc.fold_jellyfin_path_mappings(raw) is True
        assert svc.fold_jellyfin_path_mappings(raw) is False  # once only
    assert raw["jellyfin_path_mappings"] == {"/media/music": "/data/music"}
    assert [m.get("jellyfin_path") for m in raw["path_mappings"]] == ["/media/movies", "/media/shows", None]

    # The folded file, loaded, then rewritten by a run and loaded again
    folded = _write(tmp_path, raw, "folded.json")
    assert _where_jellyfin_files_go(_load(folded)) == before
    _load(folded, persist=True)
    assert _where_jellyfin_files_go(_load(folded)) == before


def test_a_v330_file_with_jellyfin_keeps_jellyfin_on(tmp_path):
    config = _load(_write(tmp_path, V330_BOTH))
    assert config.plex.enabled and config.jellyfin.enabled
    assert config.jellyfin.skip_users == ["guest"] and config.jellyfin.favorites_toggle


def test_a_wizard_jellyfin_only_file_survives_a_run(tmp_path):
    path = _write(tmp_path, WIZARD_JELLYFIN)
    first = _load(path, persist=True)
    assert not first.plex.enabled and first.jellyfin.enabled
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [(m["plex_path"], m["jellyfin_path"], m["jellyfin_library_id"]) for m in saved["path_mappings"]] == [
        ("", "/media/movies", "lib-movies"), ("", "/media/tv", "lib-tv")]
    again = _load(path)
    assert _where_jellyfin_files_go(again) == _where_jellyfin_files_go(first)
    assert _where_jellyfin_files_go(again)["/media/movies/Dune (2021)/Dune.mkv"] == "/mnt/user/movies/Dune (2021)/Dune.mkv"


@pytest.mark.critical
def test_moving_the_env_vars_mappings_changes_where_no_file_goes(tmp_path, monkeypatch):
    """With JELLYFIN_PATH_MAPPINGS set (the CA template field), the run reads the env var, not the file."""
    from web.services.settings_service import SettingsService
    monkeypatch.setenv("JELLYFIN_PATH_MAPPINGS", json.dumps({"/media/movies": "/data/movies",
                                                            "/media/shows": "/data/tv"}))
    settings = {k: v for k, v in V330_BOTH.items() if k != "jellyfin_path_mappings"}
    before = _where_jellyfin_files_go(_load(_write(tmp_path, settings)))
    assert before["/media/shows/Andor/Season 2/Andor S02E01.mkv"] == "/mnt/user/media/tv/Andor/Season 2/Andor S02E01.mkv"

    raw = copy.deepcopy(settings)
    svc = SettingsService.__new__(SettingsService)
    svc._load_raw = lambda: settings
    with patch("web.services.settings_service.DATA_DIR", tmp_path):
        assert svc.fold_jellyfin_path_mappings(raw) is True
        assert svc.fold_jellyfin_path_mappings(raw) is False
    assert [m.get("jellyfin_path") for m in raw["path_mappings"]] == ["/media/movies", "/media/shows", None]
    assert "jellyfin_path_mappings" not in raw  # the env var is left to the container
    assert _where_jellyfin_files_go(_load(_write(tmp_path, raw, "folded.json"))) == before
