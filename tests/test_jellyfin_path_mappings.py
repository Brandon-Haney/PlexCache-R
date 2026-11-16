"""Jellyfin paths on the path mappings themselves (docs/media-servers-design.md, section 4).

Jellyfin used to reach real paths only through jellyfin_path_mappings, which
translate into Plex paths, so a library had to exist in Plex. A path mapping
now has its own jellyfin_path:

- with a plex_path, Jellyfin's files become that Plex path, so a file both
  servers report keeps one tracker entry, as before;
- without one (a library only Jellyfin has), they become the real path, which
  the run passes through unchanged.

The older jellyfin_path_mappings still apply, a disabled library is skipped
without the "unmapped path" notice, and editing a mapping in the Web UI keeps
the Jellyfin fields it doesn't show yet.
"""

import json
import logging
import sys
from unittest.mock import MagicMock, patch

import pytest

for _mod in ("fcntl", "pwd", "grp", "plexapi", "plexapi.server", "plexapi.video", "plexapi.myplex",
             "apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.background",
             "apscheduler.triggers", "apscheduler.triggers.cron", "apscheduler.triggers.interval"):
    sys.modules.setdefault(_mod, MagicMock())

from core.config import JellyfinConfig, PathMapping  # noqa: E402
from core.file_operations import MultiPathModifier  # noqa: E402
from core.jellyfin_api import JellyfinManager  # noqa: E402


def _app(mappings, legacy=None):
    from core.app import PlexCacheApp
    app = object.__new__(PlexCacheApp)
    app.config_manager = MagicMock()
    app.config_manager.jellyfin = JellyfinConfig(url="http://jf", api_key="k", path_mappings=legacy or {})
    app.config_manager.paths.path_mappings = mappings
    return app


MOVIES = PathMapping(name="Movies", plex_path="/data/movies/", real_path="/mnt/user/movies/",
                     cache_path="/mnt/cache/movies/", jellyfin_path="/media/movies")
ANIME = PathMapping(name="Anime", plex_path="", real_path="/mnt/user/anime/",
                    cache_path="/mnt/cache/anime/", jellyfin_path="/media/anime")
OFF = PathMapping(name="Kids", plex_path="/data/kids/", real_path="/mnt/user/kids/",
                  jellyfin_path="/media/kids", enabled=False)


# -- the translation table --------------------------------------------------------

def test_a_mapping_with_a_plex_path_shares_it():
    table, skipped = _app([MOVIES])._jellyfin_paths()
    assert table == {"/media/movies": "/data/movies/"} and skipped == []


def test_a_jellyfin_only_library_goes_to_the_real_path():
    table, _ = _app([ANIME])._jellyfin_paths()
    assert table == {"/media/anime": "/mnt/user/anime/"}


def test_the_older_setting_still_applies_and_a_mapping_wins():
    table, _ = _app([MOVIES], legacy={"/media/movies": "/old/movies/", "/media/tv": "/data/tv/"})._jellyfin_paths()
    assert table == {"/media/movies": "/data/movies/", "/media/tv": "/data/tv/"}


def test_a_disabled_library_is_skipped():
    table, skipped = _app([OFF])._jellyfin_paths()
    assert table == {} and skipped == ["/media/kids"]


def test_no_jellyfin_paths_warns(caplog):
    app = _app([PathMapping(name="Movies", plex_path="/data/movies/", real_path="/mnt/user/movies/")])
    with caplog.at_level(logging.WARNING), patch("core.app.JellyfinManager"):
        app._init_jellyfin_manager()
    assert "No Jellyfin paths are mapped" in caplog.text


# -- Jellyfin's paths, end to end --------------------------------------------------

def _manager(table, skipped=()):
    return JellyfinManager("http://jf", "k", path_mappings=table, skip_prefixes=list(skipped), session=MagicMock())


@pytest.mark.critical
def test_jellyfin_paths_reach_the_real_files():
    table, skipped = _app([MOVIES, ANIME, OFF])._jellyfin_paths()
    manager = _manager(table, skipped)
    modifier = MultiPathModifier([MOVIES, ANIME, OFF])
    paths = manager._item_paths({"Path": "/media/movies/Film (2020)/Film.mkv"}) + \
        manager._item_paths({"Path": "/media/anime/Show/S01E01.mkv"})
    assert paths == ["/data/movies/Film (2020)/Film.mkv", "/mnt/user/anime/Show/S01E01.mkv"]
    assert modifier.modify_file_paths(paths) == ["/mnt/user/movies/Film (2020)/Film.mkv",
                                                 "/mnt/user/anime/Show/S01E01.mkv"]


@pytest.mark.critical
def test_a_jellyfin_only_mapping_never_catches_plex_paths():
    """An empty plex_path would match every path ("".startswith), so an unmapped
    Plex path would get the Jellyfin library's real path glued on the front."""
    modifier = MultiPathModifier([MOVIES, ANIME])
    assert modifier.convert_plex_to_real("/data/tv/Show/e1.mkv") == ("/data/tv/Show/e1.mkv", None)
    assert modifier.modify_file_paths(["/data/tv/Show/e1.mkv"]) == ["/data/tv/Show/e1.mkv"]
    assert modifier.get_mapping_for_path("/data/tv/Show/e1.mkv") is None
    assert modifier.get_mapping_for_path("/mnt/user/anime/Show/e1.mkv") is ANIME
    disabled = PathMapping(name="Old", plex_path="", real_path="/mnt/user/old/", enabled=False)
    MultiPathModifier([MOVIES, disabled]).modify_file_paths(["/data/tv/e1.mkv"])  # no disabled-skip count


def test_a_disabled_library_is_skipped_quietly(caplog):
    manager = _manager({}, skipped=["/media/kids"])
    with caplog.at_level(logging.INFO):
        assert manager._item_paths({"Path": "/media/kids/Film.mkv"}) == []
        assert manager._item_paths({"Path": "/other/Film.mkv"}) == []
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1 and "/other/" in infos[0]


def test_config_reads_the_jellyfin_fields(tmp_path, monkeypatch):
    from core.config import ConfigManager
    for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    real, cache = tmp_path / "media", tmp_path / "cache"
    real.mkdir(); cache.mkdir()
    settings = {
        "jellyfin_url": "http://jf", "jellyfin_api_key": "k", "jellyfin_enabled": True,
        "number_episodes": 5, "days_to_monitor": 99, "watchlist_toggle": False, "watchlist_episodes": 3,
        "watched_move": True, "max_concurrent_moves_array": 2, "max_concurrent_moves_cache": 5,
        "cache_dir": str(cache),
        "path_mappings": [{"name": "Anime", "plex_path": "", "real_path": str(real), "cache_path": str(cache),
                           "enabled": True, "jellyfin_path": " /media/anime ", "jellyfin_library_id": "abc123"}],
    }
    path = tmp_path / "plexcache_settings.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    config = ConfigManager(str(path))
    config.load_config(persist_updates=False)
    mapping = config.paths.path_mappings[0]
    assert (mapping.jellyfin_path, mapping.jellyfin_library_id) == ("/media/anime", "abc123")
    assert not config.plex.enabled


# -- editing a mapping in the Web UI ------------------------------------------------

@pytest.fixture
def svc(tmp_path):
    settings_file = tmp_path / "plexcache_settings.json"
    settings_file.write_text(json.dumps({"path_mappings": [{
        "name": "Movies", "plex_path": "/data/movies/", "real_path": "/mnt/user/movies/", "section_id": 3,
        "jellyfin_path": "/media/movies", "jellyfin_library_id": "abc"}]}), encoding="utf-8")
    for name in [n for n, m in sys.modules.items() if n.startswith("web") and isinstance(m, MagicMock)]:
        del sys.modules[name]
    with patch("web.services.settings_service.SETTINGS_FILE", settings_file), \
         patch("web.services.settings_service.DATA_DIR", tmp_path):
        from web.services.settings_service import SettingsService
        service = SettingsService()
        with patch.object(service, "invalidate_plex_cache"):
            yield service


def test_editing_a_mapping_keeps_the_jellyfin_fields(svc):
    assert svc.update_path_mapping(0, {"name": "Films", "plex_path": "/data/films/", "real_path": "/mnt/user/films/"})
    saved = svc.get_path_mappings()[0]
    assert saved["name"] == "Films"
    assert (saved["section_id"], saved["jellyfin_path"], saved["jellyfin_library_id"]) == (3, "/media/movies", "abc")


def test_jellyfin_path_is_trimmed_like_the_other_paths(svc):
    assert svc._sanitize_path_mapping({"jellyfin_path": " /media/movies "})["jellyfin_path"] == "/media/movies"


# -- the settings file after a run ------------------------------------------------
# Every CLI or scheduled run loads the config with persist_updates=True, which
# rewrites path_mappings. That rewrite must keep the Jellyfin fields and an
# empty plex_path: "/" would match every path and send each Jellyfin-only
# library's files to the first such library's real path.

def _run_load(tmp_path, mappings, **extra):
    from core.config import ConfigManager
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(exist_ok=True)
    settings = {
        "PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "tok", "number_episodes": 5, "valid_sections": [1],
        "days_to_monitor": 99, "users_toggle": False, "watchlist_toggle": False, "watchlist_episodes": 3,
        "watched_move": True, "max_concurrent_moves_array": 2, "max_concurrent_moves_cache": 5,
        "cache_dir": str(cache_dir), "path_mappings": mappings, **extra,
    }
    path = tmp_path / "plexcache_settings.json"
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    config = ConfigManager(str(path))
    config.load_config()  # persist_updates=True, as a run does
    return config, json.loads(path.read_text(encoding="utf-8"))["path_mappings"]


JELLYFIN_ONLY = [
    {"name": "Anime", "plex_path": "", "real_path": "/mnt/user/anime/", "cache_path": "/mnt/cache/anime/",
     "cacheable": True, "enabled": True, "jellyfin_path": "/media/anime", "jellyfin_library_id": "lib-anime",
     "jellyfin_enabled": True},
    {"name": "Kids", "plex_path": "", "real_path": "/mnt/user/kids/", "cache_path": "/mnt/cache/kids/",
     "cacheable": True, "enabled": True, "jellyfin_path": "/media/kids", "jellyfin_library_id": "lib-kids",
     "jellyfin_enabled": False},
]


@pytest.mark.critical
def test_a_run_keeps_the_jellyfin_fields_and_an_empty_plex_path(tmp_path):
    config, saved = _run_load(tmp_path, JELLYFIN_ONLY, plex_enabled=False, jellyfin_enabled=True,
                              jellyfin_url="http://jf:8096", jellyfin_api_key="key")
    assert [(m["plex_path"], m["jellyfin_path"], m["jellyfin_library_id"], m["jellyfin_enabled"]) for m in saved] == [
        ("", "/media/anime", "lib-anime", True), ("", "/media/kids", "lib-kids", False)]
    assert [m.plex_path for m in config.paths.path_mappings] == ["", ""]


@pytest.mark.critical
def test_two_jellyfin_only_libraries_keep_their_own_real_paths(tmp_path):
    both_on = [dict(m, jellyfin_enabled=True) for m in JELLYFIN_ONLY]
    config, _ = _run_load(tmp_path, both_on, plex_enabled=False, jellyfin_enabled=True,
                          jellyfin_url="http://jf:8096", jellyfin_api_key="key")
    from core.jellyfin_api import jellyfin_path_table
    table, _ = jellyfin_path_table(config.jellyfin, config.paths.path_mappings)
    assert table == {"/media/anime": "/mnt/user/anime/", "/media/kids": "/mnt/user/kids/"}
    jellyfin = JellyfinManager("http://jf", "k", path_mappings=table)
    modifier = MultiPathModifier(config.paths.path_mappings)
    real, _ = modifier.convert_plex_to_real(jellyfin.to_plex_path("/media/kids/Bluey/S01E01.mkv"))
    assert real == "/mnt/user/kids/Bluey/S01E01.mkv"
    # A Plex path no mapping covers stays unmapped, not caught by an empty plex_path
    assert modifier.convert_plex_to_real("/data/other/film.mkv")[0] in (None, "/data/other/film.mkv")


def test_a_plex_only_file_is_rewritten_as_before(tmp_path):
    _, saved = _run_load(tmp_path, [{"name": "Movies", "plex_path": "/data/movies/", "real_path": "/mnt/user/movies/",
                                     "cache_path": "/mnt/cache/movies/", "cacheable": True, "enabled": True,
                                     "section_id": 1}])
    assert saved == [{"name": "Movies", "plex_path": "/data/movies/", "real_path": "/mnt/user/movies/",
                      "cache_path": "/mnt/cache/movies/", "host_cache_path": None, "cacheable": True,
                      "enabled": True, "section_id": 1}]
