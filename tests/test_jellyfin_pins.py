"""Pins on Jellyfin items (docs/media-servers-design.md, phase 2).

A pin made from Jellyfin is stored under the Jellyfin item id with source
"jellyfin" and resolved by Jellyfin: a movie or episode to its chosen version,
a show or season to its episodes, by the same version rule as Plex pins. It is
never pruned, since Jellyfin gives a replaced file a new id. Seasons and
episodes come from the TV endpoints (/Shows/{id}/Seasons, /Shows/{id}/Episodes
?seasonId=): without a user, /Items with a season as parentId misses episodes
that sit directly in the series folder. The run applies
each server's pins, the pin picker searches and expands both servers, and the
budget, Recently Added and Cached Files see Jellyfin pins like Plex ones.
"""

import json
import logging
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault('fcntl', MagicMock())
for _mod in ['apscheduler', 'apscheduler.schedulers', 'apscheduler.schedulers.background',
             'apscheduler.triggers', 'apscheduler.triggers.cron', 'apscheduler.triggers.interval',
             'plexapi', 'plexapi.server']:
    sys.modules.setdefault(_mod, MagicMock())

from core.jellyfin_api import JellyfinManager  # noqa: E402
from core.pinned_media import PinnedMediaTracker, resolve_jellyfin_pins  # noqa: E402

FILM_ID, SHOW_ID, SEASON1, SEASON2 = "f" * 32, "a" * 32, "b" * 32, "c" * 32
EP1, EP2, EP3 = "1" * 32, "2" * 32, "3" * 32


def _video(width, height):
    return [{"Type": "Video", "Width": width, "Height": height}]


FILM = {"Id": FILM_ID, "Name": "Dune", "Type": "Movie", "ProductionYear": 2021,
        "Path": "/media/movies/Dune/Dune 2160p.mkv",
        "MediaSources": [
            {"Path": "/media/movies/Dune/Dune 1080p.mkv", "Size": 12, "MediaStreams": _video(1920, 800)},
            {"Path": "/media/movies/Dune/Dune 2160p.mkv", "Size": 60, "MediaStreams": _video(3840, 1608)}]}
SHOW = {"Id": SHOW_ID, "Name": "Shogun", "Type": "Series", "ProductionYear": 2024, "Path": "/media/tv/Shogun"}
EPISODES = [
    {"Id": eid, "Name": f"Chapter {n}", "Type": "Episode", "SeriesName": "Shogun", "SeriesId": SHOW_ID,
     "SeasonId": season, "ParentIndexNumber": s, "IndexNumber": n,
     "MediaSources": [{"Path": f"/media/tv/Shogun/S0{s}E0{n}.mkv", "Size": 10 * n}]}
    for eid, season, s, n in ((EP1, SEASON1, 1, 1), (EP2, SEASON1, 1, 2), (EP3, SEASON2, 2, 3))]
SEASONS = [{"Id": SEASON1, "Name": "Season 1", "Type": "Season", "IndexNumber": 1, "SeriesName": "Shogun",
            "SeriesId": SHOW_ID, "ChildCount": 2},
           {"Id": SEASON2, "Name": "Season 2", "Type": "Season", "IndexNumber": 2, "SeriesName": "Shogun",
            "SeriesId": SHOW_ID, "ChildCount": 1}]
EVERYTHING = {item["Id"]: item for item in [FILM, SHOW] + EPISODES + SEASONS}
LIBRARIES = [
    {"Name": "Movies", "ItemId": "lib-movies", "CollectionType": "movies", "Locations": ["/media/movies"]},
    {"Name": "Shows", "ItemId": "lib-tv", "CollectionType": "tvshows", "Locations": ["/media/tv"]},
    {"Name": "Home", "ItemId": "lib-home", "CollectionType": "homevideos", "Locations": ["/media/home"]},
]


def _answer(path, params):
    params = params or {}
    if path == "/Library/VirtualFolders":
        return LIBRARIES
    if path.startswith("/Shows/"):
        return _answer_tv(path, params)
    if path != "/Items":
        raise AssertionError(f"unexpected {path}")
    if "ids" in params:
        return {"Items": [EVERYTHING[i] for i in params["ids"].split(",") if i in EVERYTHING]}
    if "searchTerm" in params:
        term = params["searchTerm"].lower()
        stray = {"Id": "9" * 32, "Name": "Dune home video", "Type": "Movie", "Path": "/media/home/dune.mkv",
                 "MediaSources": [{"Path": "/media/home/dune.mkv", "Size": 1}]}
        return {"Items": [i for i in (FILM, SHOW, stray) if term in i["Name"].lower()]}
    raise AssertionError(f"unexpected /Items {params}")


def _answer_tv(path, params):
    """/Shows/{series}/Seasons and /Shows/{series}/Episodes[?seasonId=], as Jellyfin
    answers them without a user (TvShowsController)."""
    _, _, series, what = path.split("/")
    if what == "Seasons":
        return {"Items": [s for s in SEASONS if s["SeriesId"] == series]}
    episodes = [e for e in EPISODES if e["SeriesId"] == series]
    if "seasonId" in params:
        episodes = [e for e in episodes if e["SeasonId"] == params["seasonId"]]
    return {"Items": episodes}


def _jellyfin(calls=None):
    session = MagicMock()
    session.headers = {}

    def get(url, params=None, timeout=None):
        path = url.split("8096", 1)[1]
        if calls is not None:
            calls.append((path, dict(params or {})))
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = _answer(path, params)
        return response

    session.get.side_effect = get
    return JellyfinManager("http://jf:8096", "key", session=session,
                           path_mappings={"/media/movies": "/data/movies", "/media/tv": "/mnt/user/tv"})


# --- JellyfinManager ------------------------------------------------------------

def test_a_movie_pin_resolves_to_its_chosen_version():
    jellyfin = _jellyfin()
    assert jellyfin.resolve_pin(FILM_ID, "movie", "highest") == [("/data/movies/Dune/Dune 2160p.mkv", 60)]
    assert jellyfin.resolve_pin(FILM_ID, "movie", "1080p") == [("/data/movies/Dune/Dune 1080p.mkv", 12)]


def test_a_show_or_season_pin_resolves_to_its_episodes():
    jellyfin = _jellyfin()
    assert [p for p, _ in jellyfin.resolve_pin(SHOW_ID, "show")] == [
        "/mnt/user/tv/Shogun/S01E01.mkv", "/mnt/user/tv/Shogun/S01E02.mkv", "/mnt/user/tv/Shogun/S02E03.mkv"]
    assert jellyfin.resolve_pin(SEASON2, "season") == [("/mnt/user/tv/Shogun/S02E03.mkv", 30)]


def test_an_item_jellyfin_no_longer_has_resolves_to_none():
    assert _jellyfin().resolve_pin("0" * 32, "movie") is None
    assert _jellyfin().resolve_pin("0" * 32, "show") is None


def test_search_lists_items_of_mapped_libraries():
    hits = _jellyfin().search_items("dune")
    assert hits == [{"id": FILM_ID, "title": "Dune", "type": "movie", "year": 2021, "library": "Movies",
                     "size": 60}]  # the home video library isn't mapped
    assert [h["type"] for h in _jellyfin().search_items("shogun")] == ["show"]


def test_children_list_seasons_with_counts_and_sizes_then_episodes():
    jellyfin = _jellyfin()
    assert jellyfin.pin_children(SHOW_ID, "show") == [
        {"id": SEASON1, "title": "Season 1", "index": 1, "episode_count": 2, "size": 30},
        {"id": SEASON2, "title": "Season 2", "index": 2, "episode_count": 1, "size": 30}]
    assert [(e["id"], e["season"], e["index"], e["size"]) for e in jellyfin.pin_children(SEASON1, "season")] == [
        (EP1, 1, 1, 10), (EP2, 1, 2, 20)]


# --- core.pinned_media.resolve_jellyfin_pins ---------------------------------------

@pytest.fixture
def tracker(tmp_path):
    return PinnedMediaTracker(str(tmp_path / "pinned_media.json"))


@pytest.mark.critical
def test_resolve_never_removes_a_missing_jellyfin_pin(tracker, caplog):
    tracker.add_pin(FILM_ID, "movie", "Dune", source="jellyfin")
    tracker.add_pin("0" * 32, "episode", "Replaced episode", source="jellyfin")
    tracker.add_pin("123", "movie", "A Plex film")
    with caplog.at_level(logging.WARNING):
        resolved, missing = resolve_jellyfin_pins(_jellyfin(), tracker, "highest")
    assert resolved == [("/data/movies/Dune/Dune 2160p.mkv", FILM_ID, "movie")]
    assert missing == ["0" * 32]
    assert {p["rating_key"] for p in tracker.list_pins()} == {FILM_ID, "0" * 32, "123"}
    assert "no longer in Jellyfin" in caplog.text


def test_a_jellyfin_error_leaves_the_pin(tracker):
    tracker.add_pin(FILM_ID, "movie", "Dune", source="jellyfin")
    broken = MagicMock()
    broken.resolve_pin.side_effect = ConnectionError("refused")
    assert resolve_jellyfin_pins(broken, tracker) == ([], [])
    assert tracker.is_pinned(FILM_ID)


# --- the run ---------------------------------------------------------------------------

def _run_app(tracker, plex_manager=None, jellyfin_manager=None):
    from core.app import PlexCacheApp
    from core.config import PathMapping
    from core.file_operations import MultiPathModifier
    app = object.__new__(PlexCacheApp)
    app.config_manager = MagicMock()
    app.config_manager.plex.pinned_preferred_resolution = "highest"
    app.config_manager.cache.cache_associated_files = "none"
    app.pinned_tracker = tracker
    app.plex_manager = plex_manager
    app.jellyfin_manager = jellyfin_manager
    app.source_map, app.sibling_map, app.files_to_skip = {}, {}, []
    app.priority_manager = MagicMock()
    app.file_path_modifier = MultiPathModifier([
        PathMapping(name="Movies", plex_path="/data/movies/", real_path="/mnt/user/movies/",
                    cache_path="/mnt/cache/movies/"),
        PathMapping(name="TV", plex_path="", real_path="/mnt/user/tv/", cache_path="/mnt/cache/tv/",
                    jellyfin_path="/media/tv")])
    return app


@pytest.mark.critical
def test_the_run_applies_jellyfin_pins_without_plex(tracker, caplog):
    tracker.add_pin(SEASON2, "season", "Shogun — Season 2", source="jellyfin")
    tracker.add_pin(FILM_ID, "movie", "Dune", source="jellyfin")
    tracker.add_pin("123", "movie", "A Plex film")
    app = _run_app(tracker, jellyfin_manager=_jellyfin())
    modified = {}
    with caplog.at_level(logging.INFO):
        app._process_pinned_media(modified)
    assert set(modified) == {"/mnt/user/tv/Shogun/S02E03.mkv", "/mnt/user/movies/Dune/Dune 2160p.mkv"}
    assert app.pinned_rating_keys == {SEASON2, FILM_ID}
    assert app.source_map["/mnt/user/tv/Shogun/S02E03.mkv"] == "pinned"
    # Protected from eviction like any pin
    assert app.priority_manager.active_pinned_paths == {"/mnt/cache/tv/Shogun/S02E03.mkv",
                                                        "/mnt/cache/movies/Dune/Dune 2160p.mkv"}
    assert "1 Plex pin(s) not applied: Plex is not in use" in caplog.text


def test_with_both_servers_each_resolves_its_own_pins(tracker):
    tracker.add_pin(FILM_ID, "movie", "Dune", source="jellyfin")
    tracker.add_pin("123", "movie", "A Plex film")
    plex_manager = MagicMock()
    with patch("core.app.resolve_pins_to_paths",
               return_value=([("/data/movies/Arrival/Arrival.mkv", "123", "movie")], [])) as plex_resolve:
        app = _run_app(tracker, plex_manager=plex_manager, jellyfin_manager=_jellyfin())
        modified = {}
        app._process_pinned_media(modified)
    assert plex_resolve.call_args.kwargs["prune"] is True
    assert list(modified) == ["/mnt/user/movies/Arrival/Arrival.mkv", "/mnt/user/movies/Dune/Dune 2160p.mkv"]


def test_plex_only_runs_never_ask_jellyfin(tracker):
    tracker.add_pin("123", "movie", "A Plex film")
    with patch("core.app.resolve_pins_to_paths", return_value=([], [])), \
            patch("core.app.resolve_jellyfin_pins") as jellyfin_resolve:
        _run_app(tracker, plex_manager=MagicMock())._process_pinned_media({})
    jellyfin_resolve.assert_not_called()


# --- PinnedService ------------------------------------------------------------------------

SETTINGS = {
    "jellyfin_enabled": True, "jellyfin_url": "http://jf:8096", "jellyfin_api_key": "key",
    "plex_enabled": False, "pinned_preferred_resolution": "highest", "cache_limit": "",
    "path_mappings": [
        {"name": "Movies", "plex_path": "/data/movies/", "real_path": "/mnt/user/movies/",
         "cache_path": "/mnt/cache/movies/", "enabled": True},
        {"name": "TV", "plex_path": "", "real_path": "/mnt/user/tv/", "cache_path": "/mnt/cache/tv/",
         "enabled": True, "jellyfin_path": "/media/tv"},
    ],
}


@pytest.fixture
def service(tmp_path, monkeypatch):
    for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    settings = dict(SETTINGS)
    from web.services.pinned_service import PinnedService
    svc = PinnedService.__new__(PinnedService)
    svc._tracker = PinnedMediaTracker(str(tmp_path / "pinned_media.json"))
    svc._settings = lambda: settings
    svc._get_preference = lambda: "highest"
    svc._get_jellyfin = lambda: _jellyfin()
    svc._load_parsed_settings = lambda: {"cache_limit_bytes": 0, "min_free_space_bytes": 0,
                                         "plexcache_quota_bytes": 0}
    settings_service = MagicMock()
    settings_service.get_all.side_effect = lambda: settings
    monkeypatch.setattr("web.services.get_settings_service", lambda: settings_service)
    svc.settings = settings
    return svc


def test_pinning_a_jellyfin_item_stores_its_server(service):
    result = service.toggle_pin(SEASON1, "season", "Shogun — Season 1", source="jellyfin")
    assert result["is_pinned"] and service._tracker.get_pin(SEASON1)["source"] == "jellyfin"
    assert result["budget"]["total_pinned_bytes"] == 0  # nothing on cache yet
    assert service.toggle_pin(SEASON1, "season", "")["is_pinned"] is False  # unpin needs no server


def test_the_budget_prices_a_jellyfin_pin_from_its_files(service):
    assert service._estimate_item_bytes(SHOW_ID, "show", "jellyfin") == 60
    assert service.budget_check(additional_rating_key=FILM_ID, additional_pin_type="movie",
                                additional_source="jellyfin")["additional_bytes"] == 60


def test_an_unknown_server_is_refused(service):
    assert service.toggle_pin(FILM_ID, "movie", "Dune", source="emby")["error"] == "Unknown media server: emby"
    assert not service._tracker.is_pinned(FILM_ID)


def test_the_pin_map_includes_jellyfin_pins_and_ignores_dormant_plex_pins(service):
    service._tracker.add_pin(SEASON2, "season", "Shogun — Season 2", source="jellyfin")
    service._tracker.add_pin("123", "movie", "A Plex film")  # Plex is switched off
    assert service.resolve_all_to_plex_path_map() == {
        "/mnt/user/tv/Shogun/S02E03.mkv": (SEASON2, "season", "Shogun — Season 2")}
    # Jellyfin-only libraries give real paths; they still reach their cache path
    assert service.resolve_all_to_cache_path_map() == {"/mnt/cache/tv/Shogun/S02E03.mkv": (SEASON2, "season")}


def test_an_unreachable_jellyfin_with_pins_is_unknown(service):
    service._tracker.add_pin(FILM_ID, "movie", "Dune", source="jellyfin")
    service._get_jellyfin = lambda: None
    assert service.resolve_all_to_plex_path_map() is None
    assert service.resolve_all_to_cache_path_map() == {}


def test_search_and_expand_tag_jellyfin_items(service):
    service._get_plex_server = lambda: None
    hits = service.search("dune")
    assert [(h["rating_key"], h["source"], h["library"], h["size_display"]) for h in hits] == [
        (FILM_ID, "jellyfin", "Movies", "60 B")]
    seasons = service.expand(SHOW_ID, "show", source="jellyfin")
    assert [(s["title"], s["type"], s["episode_count"], s["source"]) for s in seasons] == [
        ("Shogun (2024) — Season 1", "season", 2, "jellyfin"), ("Shogun (2024) — Season 2", "season", 1, "jellyfin")]
    episodes = service.expand(SEASON1, "season", source="jellyfin")
    assert [(e["title"], e["season_number"], e["index"]) for e in episodes] == [
        ("Shogun — S01E01 — Chapter 1", 1, 1), ("Shogun — S01E02 — Chapter 2", 1, 2)]


def test_the_pinned_list_groups_jellyfin_episodes_under_their_show(service):
    service._get_plex_server = lambda: None
    service._tracker.add_pin(EP2, "episode", "Shogun — S01E02 — Chapter 2", source="jellyfin")
    service._tracker.add_pin(SEASON2, "season", "Shogun — Season 2", source="jellyfin")
    service._tracker.add_pin(FILM_ID, "movie", "Dune", source="jellyfin")
    groups = service.list_pins_grouped()
    assert [(g["group_title"], g["pin_count"]) for g in groups] == [("Dune (2021)", 1), ("Shogun", 2)]
    shogun = groups[1]
    assert shogun["group_rating_key"] == SHOW_ID
    assert [p["scope_text"] for p in shogun["pins"]] == ["Season 2 (1 eps)", "S01E02 — Chapter 2"]


# --- routes -------------------------------------------------------------------------------

def test_the_toggle_and_expand_routes_pass_the_server():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web.routers import pinned as pinned_router
    fake = MagicMock()
    fake.toggle_pin.return_value = {"is_pinned": True, "error": None, "budget": {}, "pinned_paths": []}
    fake.expand.return_value = []
    app = FastAPI()
    app.include_router(pinned_router.router, prefix="/api/pinned")
    with patch("web.routers.pinned.get_pinned_service", return_value=fake):
        client = TestClient(app)
        response = client.post("/api/pinned/toggle", data={"rating_key": FILM_ID, "pin_type": "movie",
                                                           "title": "Dune", "source": "jellyfin"})
        assert fake.toggle_pin.call_args.kwargs["source"] == "jellyfin"
        assert 'name="source" value="jellyfin"' in response.text  # the swapped-in button keeps it
        client.get(f"/api/pinned/expand?rating_key={SHOW_ID}&level=show&source=jellyfin")
        assert fake.expand.call_args.kwargs["source"] == "jellyfin"
        assert client.get(f"/api/pinned/expand?rating_key=1&level=show&source=emby").status_code == 422
