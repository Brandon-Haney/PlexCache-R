"""Jellyfin libraries on Settings > Libraries (docs/media-servers-design.md, section 4).

Switching a Jellyfin library on links each of its folders to a path mapping:
one already set to that Jellyfin path, one whose Plex or real path is the same
folder, or one whose real path is where the folder's files would be (so Plex's
/data/movies and Jellyfin's /media/movies share a mapping and a tracker
entry). Only a folder nothing matches gets a new, Jellyfin-only mapping.

Switching it off sets the mapping's jellyfin_enabled to false and nothing
else: a mapping shared with a Plex library stays on for Plex, and a Plex
library that's switched off isn't switched back on by Jellyfin.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.modules.setdefault('fcntl', MagicMock())
for _mod in ['apscheduler', 'apscheduler.schedulers', 'apscheduler.schedulers.background',
             'apscheduler.triggers', 'apscheduler.triggers.cron', 'apscheduler.triggers.interval',
             'plexapi', 'plexapi.server']:
    sys.modules.setdefault(_mod, MagicMock())

MOVIES_PLEX = {"name": "Movies", "plex_path": "/data/movies/", "real_path": "/mnt/user/movies/",
               "cache_path": "/mnt/cache/movies/", "cacheable": True, "enabled": True, "section_id": 1}
TV_PLEX_OFF = {"name": "TV", "plex_path": "/media/tv/", "real_path": "/mnt/user/tv/",
               "cache_path": "/mnt/cache/tv/", "cacheable": True, "enabled": False, "section_id": 2}
LIBRARIES = [
    {"id": "lib-movies", "title": "Movies", "type": "movie", "type_label": "Movies", "locations": ["/media/movies"]},
    {"id": "lib-tv", "title": "Shows", "type": "show", "type_label": "TV Shows", "locations": ["/media/tv/"]},
    {"id": "lib-anime", "title": "Anime", "type": "show", "type_label": "TV Shows", "locations": ["/media/anime"]},
]


@pytest.fixture
def make_client(tmp_path):
    patches = []

    def client(mappings):
        settings_file = tmp_path / "plexcache_settings.json"
        settings_file.write_text(json.dumps({
            "PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "tok", "cache_dir": "/mnt/cache",
            "jellyfin_url": "http://jf:8096", "jellyfin_api_key": "key", "jellyfin_enabled": True,
            "path_mappings": [dict(m) for m in mappings]}, indent=2), encoding="utf-8")
        for p in (patch("web.services.settings_service.SETTINGS_FILE", settings_file),
                  patch("web.services.settings_service.DATA_DIR", tmp_path)):
            p.start(); patches.append(p)
        from web.services.settings_service import SettingsService
        svc = SettingsService()
        svc._cached_settings = None
        p = patch.object(svc, "get_jellyfin_libraries", return_value={"items": LIBRARIES, "error": None})
        p.start(); patches.append(p)
        from web.routers import settings as settings_router
        p = patch("web.routers.settings.get_settings_service", return_value=svc)
        p.start(); patches.append(p)
        app = FastAPI()
        app.include_router(settings_router.router, prefix="/settings")
        return TestClient(app), svc

    yield client
    for p in reversed(patches):
        p.stop()


def _mappings(svc):
    return svc._load_raw()["path_mappings"]


def _by_library(svc, library_id):
    return [m for m in _mappings(svc) if m.get("jellyfin_library_id") == library_id]


@pytest.mark.critical
def test_a_folder_shared_with_plex_links_to_its_mapping(make_client):
    """Plex /data/movies/ and Jellyfin /media/movies are both /mnt/user/movies/: one mapping."""
    client, svc = make_client([MOVIES_PLEX])
    client.post("/settings/libraries/jellyfin/lib-movies/toggle")
    assert len(_mappings(svc)) == 1
    (mapping,) = _by_library(svc, "lib-movies")
    assert mapping["plex_path"] == "/data/movies/" and mapping["jellyfin_path"] == "/media/movies"
    assert mapping["section_id"] == 1 and mapping["jellyfin_enabled"] is True


def test_a_folder_only_jellyfin_has_gets_its_own_mapping(make_client):
    client, svc = make_client([MOVIES_PLEX])
    client.post("/settings/libraries/jellyfin/lib-anime/toggle")
    (mapping,) = _by_library(svc, "lib-anime")
    assert mapping["plex_path"] == "" and mapping["jellyfin_path"] == "/media/anime"
    assert mapping["real_path"] == "/mnt/user/anime/" and mapping["cache_path"] == "/mnt/cache/anime/"
    assert "section_id" not in mapping and mapping["enabled"] is True


def test_a_plex_library_switched_off_stays_off(make_client):
    """Jellyfin's /media/tv/ is Plex's /media/tv/ (same mount), whose library is off."""
    client, svc = make_client([TV_PLEX_OFF])
    page = client.post("/settings/libraries/jellyfin/lib-tv/toggle").text
    (mapping,) = _by_library(svc, "lib-tv")
    assert mapping["enabled"] is False and mapping["jellyfin_enabled"] is True
    assert "shared with a Plex library that is switched off" in page


def test_switching_off_touches_only_jellyfin(make_client):
    client, svc = make_client([MOVIES_PLEX])
    client.post("/settings/libraries/jellyfin/lib-movies/toggle")
    page = client.post("/settings/libraries/jellyfin/lib-movies/toggle").text
    (mapping,) = _by_library(svc, "lib-movies")
    assert mapping["jellyfin_enabled"] is False and mapping["enabled"] is True
    assert "library-card-disabled" in page
    client.post("/settings/libraries/jellyfin/lib-movies/toggle")
    assert len(_mappings(svc)) == 1 and _by_library(svc, "lib-movies")[0]["jellyfin_enabled"] is True


def test_editing_a_jellyfin_librarys_paths(make_client):
    client, svc = make_client([MOVIES_PLEX])
    client.post("/settings/libraries/jellyfin/lib-anime/toggle")
    client.put("/settings/libraries/jellyfin/lib-anime/paths", data={
        "jellyfin_path_0": "/media/anime/ ", "real_path_0": "/mnt/user/anime-lib/",
        "cache_path_0": "/mnt/cache/anime-lib/", "cacheable_0": "on"})
    (mapping,) = _by_library(svc, "lib-anime")
    assert (mapping["jellyfin_path"], mapping["real_path"], mapping["cache_path"]) == (
        "/media/anime", "/mnt/user/anime-lib/", "/mnt/cache/anime-lib/")
    assert "auto_fill_recognized" not in mapping


def test_jellyfin_mappings_are_not_custom_mappings(make_client):
    from web.routers.settings import _custom_mappings
    client, svc = make_client([MOVIES_PLEX, {"name": "Music", "plex_path": "/x/", "real_path": "/mnt/user/x/"}])
    client.post("/settings/libraries/jellyfin/lib-anime/toggle")
    assert [m["name"] for m in _custom_mappings(_mappings(svc))] == ["Music"]


def test_the_page_shows_the_servers_in_use(make_client):
    client, svc = make_client([MOVIES_PLEX])
    page = client.get("/settings/libraries").text
    assert 'hx-get="/settings/libraries/jellyfin"' in page and "library-server-heading" in page

    raw = svc._load_raw()
    raw.update(PLEX_URL="", PLEX_TOKEN="")
    svc._save_raw(raw)
    page = client.get("/settings/libraries").text
    assert 'id="library-cards-section"' not in page and 'id="jellyfin-library-section"' in page


def test_cards_and_a_jellyfin_that_cant_be_reached(make_client):
    client, svc = make_client([MOVIES_PLEX])
    assert client.get("/settings/libraries/jellyfin").text.count("jellyfin-library-card-") >= 3
    with patch.object(svc, "get_jellyfin_libraries", return_value={"items": [], "error": "Could not reach Jellyfin: ConnectTimeout"}):
        page = client.get("/settings/libraries/jellyfin").text
    assert "Couldn't reach Jellyfin" in page and "ConnectTimeout" in page and "retry=true" in page


USERS = [
    {"id": "u1", "name": "roger", "is_admin": True, "is_disabled": False},
    {"id": "u2", "name": "mia", "is_admin": False, "is_disabled": False},
    {"id": "u3", "name": "old", "is_admin": False, "is_disabled": True},
]


def test_jellyfin_users_are_cached_unless_skipped(make_client, monkeypatch):
    monkeypatch.delenv("JELLYFIN_SKIP_USERS", raising=False)
    client, svc = make_client([MOVIES_PLEX])
    with patch.object(svc, "get_jellyfin_users", return_value={"items": USERS, "error": None}):
        assert "Jellyfin users saved" in client.put("/settings/jellyfin/users", data={"cached_user": ["roger"]}).text
        assert svc._load_raw()["jellyfin_skip_users"] == ["mia"]  # the disabled user isn't listed
        rows = {u["name"]: u["cached"] for u in svc.jellyfin_user_rows()["users"]}
        page = client.get("/settings/users/jellyfin").text
    assert rows == {"roger": True, "mia": False, "old": False}
    assert 'value="roger"' in page and "disabled in Jellyfin" in page


def test_the_skip_env_var_wins(make_client, monkeypatch):
    monkeypatch.setenv("JELLYFIN_SKIP_USERS", "roger")
    client, svc = make_client([MOVIES_PLEX])
    with patch.object(svc, "get_jellyfin_users", return_value={"items": USERS, "error": None}):
        rows = svc.jellyfin_user_rows()
        page = client.get("/settings/users/jellyfin").text
    assert rows["skip_from_env"] and {u["name"]: u["cached"] for u in rows["users"]}["roger"] is False
    assert "JELLYFIN_SKIP_USERS" in page


def test_users_page_shows_the_servers_in_use(make_client):
    client, svc = make_client([MOVIES_PLEX])
    page = client.get("/settings/users").text
    assert 'hx-put="/settings/users"' in page and 'hx-get="/settings/users/jellyfin"' in page
    raw = svc._load_raw()
    raw.update(PLEX_URL="", PLEX_TOKEN="")
    svc._save_raw(raw)
    page = client.get("/settings/users").text
    assert 'hx-put="/settings/users"' not in page and 'hx-get="/settings/users/jellyfin"' in page


def test_older_jellyfin_path_mappings_move_onto_the_mappings(make_client, tmp_path):
    """An install set up with jellyfin_path_mappings shows its libraries as on, once."""
    client, svc = make_client([MOVIES_PLEX, {**TV_PLEX_OFF, "plex_path": "/data/tv/shows/", "enabled": True}])
    raw = svc._load_raw()
    raw["jellyfin_path_mappings"] = {"/media/movies/": "/data/movies", "/media/tv": "/data/tv/",
                                     "/media/music": "/data/music/"}
    svc._save_raw(raw)

    page = client.get("/settings/libraries/jellyfin").text
    raw = svc._load_raw()
    movies, tv = raw["path_mappings"]
    assert movies["jellyfin_path"] == "/media/movies" and movies["jellyfin_library_id"] == "lib-movies"
    assert tv["jellyfin_path"] == "/media/tv/shows"  # nested under the Plex prefix
    assert raw["jellyfin_path_mappings"] == {"/media/music": "/data/music/"}  # covered nothing: kept
    assert 'id="jellyfin-library-card-lib-movies"' in page
    assert (tmp_path / "plexcache_settings.before-jellyfin-paths.json").exists()

    client.get("/settings/libraries/jellyfin")
    assert svc._load_raw()["path_mappings"] == raw["path_mappings"]  # again: nothing changes


def test_switching_off_beats_the_older_setting():
    """The same folder in jellyfin_path_mappings must not keep a switched-off library mapped."""
    from core.app import PlexCacheApp
    from core.config import JellyfinConfig, PathMapping
    app = object.__new__(PlexCacheApp)
    app.config_manager = MagicMock()
    app.config_manager.jellyfin = JellyfinConfig(url="http://jf", api_key="k",
                                                 path_mappings={"/media/movies/": "/data/movies/"})
    app.config_manager.paths.path_mappings = [
        PathMapping(name="Movies", plex_path="/data/movies/", real_path="/mnt/user/movies/",
                    jellyfin_path="/media/movies", jellyfin_enabled=False)]
    assert app._jellyfin_paths() == ({}, ["/media/movies"])


def test_the_run_skips_a_switched_off_jellyfin_library():
    from core.app import PlexCacheApp
    from core.config import JellyfinConfig, PathMapping
    app = object.__new__(PlexCacheApp)
    app.config_manager = MagicMock()
    app.config_manager.jellyfin = JellyfinConfig(url="http://jf", api_key="k")
    app.config_manager.paths.path_mappings = [
        PathMapping(name="Movies", plex_path="/data/movies/", real_path="/mnt/user/movies/",
                    jellyfin_path="/media/movies", jellyfin_enabled=False)]
    assert app._jellyfin_paths() == ({}, ["/media/movies"])
