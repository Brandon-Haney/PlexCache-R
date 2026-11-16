"""Whole runs with Jellyfin, Plex or both (docs/media-servers-design.md, phase 1 tests).

A real PlexCacheApp, loaded from a settings file, initialised and run through
_check_active_sessions() and _process_media(): the parts of a run that ask the
media servers and decide what to cache. Only the servers are faked: Jellyfin
by its HTTP API (so JellyfinManager, the path table, the trackers and the path
conversion are the real ones), Plex by a PlexManager double. Nothing is moved.

Covers a Jellyfin-only install, both servers together, and one server down
(the run caches what the other server reported and skips move-back).
"""

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

for _mod in ("fcntl", "pwd", "grp", "plexapi", "plexapi.server", "plexapi.video", "plexapi.myplex",
             "plexapi.exceptions", "apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.background",
             "apscheduler.triggers", "apscheduler.triggers.cron", "apscheduler.triggers.interval"):
    sys.modules.setdefault(_mod, MagicMock())

from core.media_server import OnDeckItem  # noqa: E402

RECENTLY = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _episode(n):
    return {"Id": f"ep{n}" + "0" * 29, "Name": f"Episode {n}", "Type": "Episode", "SeriesName": "Frieren",
            "SeriesId": "s-frieren", "SeasonId": "season-1", "ParentIndexNumber": 1, "IndexNumber": n,
            "RunTimeTicks": 14_400_000_000, "UserData": {"LastPlayedDate": RECENTLY},
            "MediaSources": [{"Path": f"/media/anime/Frieren/S01E0{n}.mkv", "Size": 900}]}


def _movie(item_id, name, folder):
    path = f"/media/movies/{folder}/{name}.mkv"
    return {"Id": item_id, "Name": name, "Type": "Movie", "UserData": {"LastPlayedDate": RECENTLY},
            "MediaSources": [{"Path": path, "Size": 4000}]}


EPISODES = [_episode(n) for n in (1, 2, 3, 4)]
ARRIVAL = _movie("a" * 32, "Arrival", "Arrival (2016)")
DUNE = _movie("d" * 32, "Dune", "Dune (2021)")
HEAT = _movie("h" * 32, "Heat", "Heat (1995)")
USERS = [{"Id": "u-roger", "Name": "roger", "Policy": {"IsAdministrator": True, "IsDisabled": False}},
         {"Id": "u-mia", "Name": "mia", "Policy": {"IsAdministrator": False, "IsDisabled": False}}]


class FakeJellyfin:
    """Jellyfin's HTTP API as a run uses it: users, Continue Watching, Next Up, a
    series' episodes, favorites, sessions and item lookups. ``down`` refuses
    every request, like a stopped server."""

    def __init__(self, down=False):
        self.down = down
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        if self.down:
            raise requests.ConnectionError("Connection refused")
        path, params = url.split("8096", 1)[1], params or {}
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = self._answer(path, params)
        return response

    def _answer(self, path, params):
        user = params.get("userId")
        if path == "/Users":
            return USERS
        if path == "/UserItems/Resume":
            return {"Items": [ARRIVAL] if user == "u-roger" else []}
        if path == "/Shows/NextUp":
            return {"Items": [EPISODES[1]] if user == "u-mia" else []}
        if path == "/Shows/s-frieren/Episodes":
            start = next((i for i, e in enumerate(EPISODES) if e["Id"] == params.get("startItemId")), 0)
            return {"Items": EPISODES[start:]}
        if path == "/Sessions":
            return [{"UserId": "u-mia", "NowPlayingItem": EPISODES[1]}]
        if path == "/Items":
            if "ids" in params:
                return {"Items": [i for i in [ARRIVAL, DUNE, HEAT] + EPISODES if i["Id"] in params["ids"].split(",")]}
            if params.get("isFavorite") == "true":
                return {"Items": [DUNE] if user == "u-roger" else []}
        raise AssertionError(f"FakeJellyfin: unexpected {path} {params}")


SETTINGS = {
    "number_episodes": 2, "valid_sections": [1], "days_to_monitor": 7, "users_toggle": False,
    "watchlist_toggle": True, "watchlist_episodes": 1, "watched_move": True,
    "max_concurrent_moves_array": 1, "max_concurrent_moves_cache": 1, "cache_dir": "/mnt/cache/",
    "cache_associated_files": "none", "create_plexcached_backups": True, "exit_if_active_session": False,
    "jellyfin_url": "http://jf:8096", "jellyfin_api_key": "key", "jellyfin_enabled": True,
    "jellyfin_favorites_toggle": True,
    "path_mappings": [
        {"name": "Movies", "plex_path": "/data/movies/", "real_path": "/mnt/user/movies/",
         "cache_path": "/mnt/cache/movies/", "cacheable": True, "enabled": True, "section_id": 1,
         "jellyfin_path": "/media/movies"},
        {"name": "Anime", "plex_path": "", "real_path": "/mnt/user/anime/", "cache_path": "/mnt/cache/anime/",
         "cacheable": True, "enabled": True, "jellyfin_path": "/media/anime", "jellyfin_library_id": "lib-anime"},
    ],
}
PLEX = {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "tok", "plex_enabled": True}
JELLYFIN_ONLY = {"plex_enabled": False}


def _plex_double():
    plex = MagicMock(name="PlexManager()")
    plex.get_on_deck_media.return_value = [
        OnDeckItem("/data/movies/Arrival (2016)/Arrival.mkv", "anna", None, True, "101"),
        OnDeckItem("/data/movies/Sicario (2015)/Sicario.mkv", "anna", None, True, "102"),
    ]
    plex.get_watchlist_media.return_value = []
    plex.is_ondeck_data_complete.return_value = True
    plex.is_watchlist_data_complete.return_value = True
    plex.get_active_sessions.return_value = []
    return plex


@pytest.fixture
def run(tmp_path, monkeypatch):
    for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_SKIP_USERS"):
        monkeypatch.delenv(key, raising=False)

    def run(settings, jellyfin=None, plex=None, pins=None):
        from core.app import PlexCacheApp
        data = tmp_path / "data"
        data.mkdir(exist_ok=True)
        if pins:
            from core.pinned_media import PinnedMediaTracker
            tracker = PinnedMediaTracker(str(data / "pinned_media.json"))
            for key, pin_type, title, source in pins:
                tracker.add_pin(key, pin_type, title, source=source)
        settings_file = tmp_path / "plexcache_settings.json"
        settings_file.write_text(json.dumps({**SETTINGS, **settings}, indent=2), encoding="utf-8")
        app = PlexCacheApp(str(settings_file), dry_run=True)
        app.config_manager.load_config(persist_updates=False)
        app.config_manager.paths.script_folder = str(tmp_path)
        app.config_manager.paths.data_folder = str(data)
        app.system_detector.is_unraid = False
        app.system_detector.is_docker = False
        with patch("core.jellyfin_api.requests.Session", return_value=jellyfin or FakeJellyfin()), \
                patch("core.app.PlexManager", return_value=plex or _plex_double()):
            app._initialize_components()
            app._connect_to_plex()
            app._check_active_sessions()
            app._process_media()
        return app

    return run


def _real(*names):
    return [f"/mnt/user/{name}" for name in names]


def _unique(paths):
    """The run's list without repeats, in order. _process_watchlist returns each
    watchlist file in both its server and real form, so the final conversion
    lists it twice (upstream since 2025, Plex too). filter_files() and the mover
    drop the repeat before anything moves; only the [FETCH] count is high."""
    return list(dict.fromkeys(paths))


@pytest.mark.critical
def test_a_jellyfin_only_run(run, caplog):
    pins = [(HEAT["Id"], "movie", "Heat", "jellyfin")]
    with caplog.at_level(logging.INFO):
        app = run(JELLYFIN_ONLY, pins=pins)
    assert app.plex_manager is None
    # Pinned first, then Continue Watching / Next Up (current + next 2), then favorites
    assert _unique(app.all_active_media) == _real(
        "movies/Heat (1995)/Heat.mkv",
        "movies/Arrival (2016)/Arrival.mkv",
        "anime/Frieren/S01E02.mkv", "anime/Frieren/S01E03.mkv", "anime/Frieren/S01E04.mkv",
        "movies/Dune (2021)/Dune.mkv")
    assert app.source_map["/mnt/user/movies/Heat (1995)/Heat.mkv"] == "pinned"
    assert app.source_map["/mnt/user/anime/Frieren/S01E03.mkv"] == "ondeck"
    assert app.source_map["/mnt/user/movies/Dune (2021)/Dune.mkv"] == "watchlist"
    # What mia is watching is skipped this run
    assert app.files_to_skip == ["/mnt/user/anime/Frieren/S01E02.mkv"]
    # Trackers name the server and the user, keyed by real path
    ondeck = app.ondeck_tracker._data
    assert ondeck["/mnt/user/anime/Frieren/S01E02.mkv"]["users"] == ["mia"]
    assert ondeck["/mnt/user/movies/Arrival (2016)/Arrival.mkv"]["sources"] == ["jellyfin"]
    watchlist = app.watchlist_tracker._data
    assert watchlist["/data/movies/Dune (2021)/Dune.mkv"]["sources"] == ["jellyfin"]  # Plex-path keys
    # Every server answered, so move-back wasn't held
    assert "Skipping array restore" not in caplog.text


def test_both_servers_share_one_entry_per_file(run):
    plex = _plex_double()
    # Plex names the same Arrival file under its own path
    plex.get_on_deck_media.return_value[0] = OnDeckItem(
        "/data/movies/Arrival (2016)/Arrival.mkv", "anna", None, True, "101")
    app = run(PLEX, plex=plex)
    assert app.plex_manager is plex and app.jellyfin_manager is not None
    active = _unique(app.all_active_media)
    assert active.count("/mnt/user/movies/Arrival (2016)/Arrival.mkv") == 1
    assert "/mnt/user/movies/Sicario (2015)/Sicario.mkv" in active  # Plex only
    assert "/mnt/user/anime/Frieren/S01E03.mkv" in active  # Jellyfin only
    entry = app.ondeck_tracker._data["/mnt/user/movies/Arrival (2016)/Arrival.mkv"]
    assert sorted(entry["users"]) == ["anna", "roger"] and sorted(entry["sources"]) == ["jellyfin", "plex"]


@pytest.mark.critical
def test_jellyfin_down_caches_plexs_items_and_holds_move_back(run, caplog):
    with caplog.at_level(logging.WARNING):
        app = run(PLEX, jellyfin=FakeJellyfin(down=True))
    assert _unique(app.all_active_media) == _real(
        "movies/Arrival (2016)/Arrival.mkv", "movies/Sicario (2015)/Sicario.mkv")
    assert "Skipping array restore - Jellyfin OnDeck data incomplete" in caplog.text


def test_plex_unreachable_holds_move_back_and_jellyfin_still_counts(run, caplog):
    plex = _plex_double()
    plex.get_on_deck_media.return_value = []
    plex.is_ondeck_data_complete.return_value = False
    with caplog.at_level(logging.WARNING):
        app = run(PLEX, plex=plex)
    assert "/mnt/user/anime/Frieren/S01E03.mkv" in app.all_active_media
    assert "Skipping array restore - OnDeck data incomplete (Plex server unreachable)" in caplog.text


def test_jellyfin_only_with_jellyfin_down_caches_nothing_and_moves_nothing_back(run, caplog):
    with caplog.at_level(logging.WARNING):
        app = run(JELLYFIN_ONLY, jellyfin=FakeJellyfin(down=True))
    assert app.all_active_media == []
    assert "Skipping array restore - Jellyfin OnDeck data incomplete" in caplog.text
