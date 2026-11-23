"""Settings written by a newer version stay intact when this one saves them.

A run rewrites the settings file on every load, and the Web UI rewrites path
mappings when a library is edited. Both used to rebuild each mapping from the
fields this version knows, so going back from a newer version silently dropped
the rest, and the loader turned an empty plex_path into "/", a prefix every
Plex path starts with. Now:

- a mapping's fields this version doesn't read are written back as they were;
- an empty plex_path stays empty and never matches a Plex path;
- a v3.3.0 settings file is written exactly as before.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

for _mod in ("fcntl", "pwd", "grp", "plexapi", "plexapi.server", "plexapi.myplex",
             "apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.background",
             "apscheduler.triggers", "apscheduler.triggers.cron", "apscheduler.triggers.interval"):
    sys.modules.setdefault(_mod, MagicMock())

from core.config import ConfigManager, PathMapping  # noqa: E402
from core.file_operations import MultiPathModifier  # noqa: E402

MOVIES = {"name": "Movies", "plex_path": "/data/movies/", "real_path": "/mnt/user/movies/",
          "cache_path": "/mnt/cache/movies/", "host_cache_path": "/mnt/cache/movies/",
          "cacheable": True, "enabled": True, "section_id": 1}
BASE = {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "tok", "valid_sections": [1], "users_toggle": False,
        "number_episodes": 5, "days_to_monitor": 99, "watchlist_toggle": False, "watchlist_episodes": 3,
        "watched_move": True, "max_concurrent_moves_array": 2, "max_concurrent_moves_cache": 5,
        "cache_dir": "/mnt/cache/", "exit_if_active_session": False}
# What a newer version may write: its own mapping fields, and a library the Plex server doesn't have
NEWER = {**BASE, "path_mappings": [
    {**MOVIES, "jellyfin_path": "/media/movies", "jellyfin_library_id": "lib-movies", "jellyfin_enabled": True},
    {"name": "Anime", "plex_path": "", "real_path": "/mnt/user/anime/", "cache_path": "/mnt/cache/anime/",
     "host_cache_path": "/mnt/cache/anime/", "cacheable": True, "enabled": True, "section_id": None,
     "jellyfin_path": "/media/anime"},
]}


def _rewrite(tmp_path, settings):
    """Load the file as a run does (which saves it back) and return what was saved."""
    path = tmp_path / "plexcache_settings.json"
    path.write_text(json.dumps(settings, indent=4), encoding="utf-8")
    config = ConfigManager(str(path))
    config.load_config(persist_updates=True)
    return config, json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.critical
def test_a_run_keeps_a_newer_versions_mapping_fields(tmp_path):
    config, saved = _rewrite(tmp_path, NEWER)
    assert saved["path_mappings"] == NEWER["path_mappings"]
    anime = config.paths.path_mappings[1]
    assert anime.plex_path == "" and anime.extra == {"jellyfin_path": "/media/anime"}
    _, again = _rewrite(tmp_path, saved)  # and again on the next run
    assert again["path_mappings"] == NEWER["path_mappings"]


def test_a_v330_file_is_written_as_before(tmp_path):
    _, saved = _rewrite(tmp_path, {**BASE, "path_mappings": [MOVIES]})
    assert saved["path_mappings"] == [MOVIES]


@pytest.mark.critical
def test_a_library_without_a_plex_path_never_takes_a_plex_path():
    """With "/" (as the loader used to make it) every unmapped Plex path landed in Anime's folder."""
    modifier = MultiPathModifier([
        PathMapping(name="Movies", plex_path="/data/movies/", real_path="/mnt/user/movies/"),
        PathMapping(name="Anime", plex_path="", real_path="/mnt/user/anime/"),
        PathMapping(name="Old", plex_path="", real_path="/mnt/user/old/", enabled=False),
    ])
    assert modifier.convert_plex_to_real("/data/movies/Dune.mkv") == ("/mnt/user/movies/Dune.mkv",
                                                                      modifier.mappings[0])
    assert modifier.convert_plex_to_real("/data/tv/Andor/S01E01.mkv") == ("/data/tv/Andor/S01E01.mkv", None)
    # Its real paths still resolve to it
    assert modifier.convert_plex_to_real("/mnt/user/anime/Frieren/S01E01.mkv")[1].name == "Anime"
    assert modifier.get_mapping_for_path("/data/tv/Andor/S01E01.mkv") is None
    assert modifier.get_mapping_for_path("/mnt/user/anime/Frieren/S01E01.mkv").name == "Anime"


# --- Web UI edits ---------------------------------------------------------------

@pytest.fixture
def service(tmp_path):
    settings_file = tmp_path / "plexcache_settings.json"
    settings_file.write_text(json.dumps(NEWER, indent=2), encoding="utf-8")
    with patch("web.services.settings_service.SETTINGS_FILE", settings_file), \
            patch("web.services.settings_service.DATA_DIR", tmp_path):
        from web.services.settings_service import SettingsService
        svc = SettingsService()
        svc._cached_settings = None
        library = {"id": 1, "title": "Movies", "type": "movie", "type_label": "Movies",
                   "locations": ["/data/movies/"]}
        with patch.object(svc, "get_plex_libraries", return_value=[library]):
            yield svc


def test_editing_a_mapping_keeps_the_fields_it_doesnt_show(service):
    edited = {k: v for k, v in NEWER["path_mappings"][1].items() if k != "jellyfin_path"}
    assert service.update_path_mapping(1, {**edited, "cache_path": "/mnt/cache/anime2/"})
    anime = service._load_raw()["path_mappings"][1]
    assert anime["cache_path"] == "/mnt/cache/anime2/" and anime["jellyfin_path"] == "/media/anime"


def test_saving_a_library_card_keeps_the_fields_it_doesnt_show(service):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web.routers import settings as settings_router
    app = FastAPI()
    app.include_router(settings_router.router, prefix="/settings")
    form = {"name_0": "Movies", "plex_path_0": "/data/movies/", "real_path_0": "/mnt/user/movies/",
            "cache_path_0": "/mnt/cache/films/", "host_cache_path_0": "", "cacheable_0": "on"}
    with patch("web.routers.settings.get_settings_service", return_value=service):
        assert TestClient(app).put("/settings/libraries/1/paths", data=form).status_code == 200
    movies = service._load_raw()["path_mappings"][0]
    assert movies["cache_path"] == "/mnt/cache/films/"
    assert (movies["jellyfin_path"], movies["jellyfin_library_id"]) == ("/media/movies", "lib-movies")
