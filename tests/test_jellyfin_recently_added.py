"""Recently Added from Jellyfin (docs/media-servers-design.md, phase 2).

JellyfinManager.get_recently_added_media() asks each mapped movie or TV
library for its newest items (/Items sorted by DateCreated), keeps those added
within N days, chooses one version per item by the rule pins use, and
translates paths the way the run does. RecentlyAddedService merges Plex and
Jellyfin, Plex first: a file both report is one row, Jellyfin's libraries are
marked when both servers are in use, and a server that fails is named while
the other's items still show.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault('fcntl', MagicMock())
for _mod in ['apscheduler', 'apscheduler.schedulers', 'apscheduler.schedulers.background',
             'apscheduler.triggers', 'apscheduler.triggers.cron', 'apscheduler.triggers.interval',
             'plexapi', 'plexapi.server']:
    sys.modules.setdefault(_mod, MagicMock())

from core.jellyfin_api import JellyfinManager, jellyfin_path_table  # noqa: E402
from core.media_server import RecentlyAddedItem, RecentlyAddedResult  # noqa: E402

NOW = datetime.now(timezone.utc)


def _stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime('%Y-%m-%dT%H:%M:%S.0000000Z')


def _video(width, height):
    return [{"Type": "Video", "Width": width, "Height": height}, {"Type": "Audio"}]


LIBRARIES = [
    {"Name": "Movies", "ItemId": "lib-movies", "CollectionType": "movies", "Locations": ["/media/movies"]},
    {"Name": "Anime", "ItemId": "lib-anime", "CollectionType": "tvshows", "Locations": ["/media/anime/"]},
    {"Name": "Music", "ItemId": "lib-music", "CollectionType": "music", "Locations": ["/media/music"]},
    {"Name": "Unmapped", "ItemId": "lib-other", "CollectionType": "movies", "Locations": ["/elsewhere"]},
]
DUNE = {
    "Id": "a" * 32, "Name": "Dune", "Type": "Movie", "DateCreated": _stamp(1),
    "MediaSources": [
        {"Path": "/media/movies/Dune/Dune 1080p.mkv", "Size": 12_000, "Bitrate": 9_000_000,
         "MediaStreams": _video(1920, 800)},
        {"Path": "/media/movies/Dune/Dune 2160p.mkv", "Size": 60_000, "Bitrate": 60_000_000,
         "MediaStreams": _video(3840, 1608)},
    ],
}
OLD_FILM = {"Id": "b" * 32, "Name": "Old", "Type": "Movie", "DateCreated": _stamp(30),
            "MediaSources": [{"Path": "/media/movies/Old/Old.mkv", "Size": 5}]}
EPISODE = {"Id": "c" * 32, "Name": "Pilot", "Type": "Episode", "SeriesName": "Frieren", "SeriesId": "s1",
           "ParentIndexNumber": 1, "IndexNumber": 1, "DateCreated": _stamp(0.5),
           "MediaSources": [{"Path": "/media/anime/Frieren/S01E01.mkv", "Size": 900}]}


class FakeJellyfin:
    """Answers the requests get_recently_added_media() makes."""

    def __init__(self, fail_library=None):
        self.fail_library = fail_library
        self.calls = []

    def get(self, url, params=None, timeout=None):
        path = url.split("8096", 1)[1]
        self.calls.append((path, dict(params or {})))
        response = MagicMock()
        response.raise_for_status.return_value = None
        if path == "/Library/VirtualFolders":
            response.json.return_value = LIBRARIES
        elif path == "/Items":
            library = params["parentId"]
            if library == self.fail_library:
                import requests
                response.raise_for_status.side_effect = requests.HTTPError("500")
            assert params["sortBy"] == "DateCreated" and params["sortOrder"] == "Descending"
            items = {"lib-movies": [DUNE, OLD_FILM], "lib-anime": [EPISODE]}.get(library, [])
            response.json.return_value = {"Items": items}
        return response


def _manager(fake, mappings=None):
    session = MagicMock()
    session.headers = {}
    session.get.side_effect = fake.get
    return JellyfinManager("http://jf:8096", "key", session=session, path_mappings=mappings or {
        "/media/movies": "/data/movies", "/media/anime": "/mnt/user/anime"})


def test_newest_items_of_mapped_libraries_within_the_window():
    fake = FakeJellyfin()
    result = _manager(fake).get_recently_added_media(days_to_monitor=7, max_items=50)
    assert [i.title for i in result.items] == ["Pilot", "Dune"]  # newest first, Old is 30 days old
    asked = [p["parentId"] for path, p in fake.calls if path == "/Items"]
    assert asked == ["lib-movies", "lib-anime"]  # not music, not the unmapped library
    pilot, dune = result.items
    assert pilot.file_path == "/mnt/user/anime/Frieren/S01E01.mkv" and pilot.source == "jellyfin"
    assert pilot.episode_info == {"show": "Frieren", "season": 1, "episode": 1} and pilot.show_rating_key == "s1"
    assert dune.rating_key == "a" * 32 and dune.library_title == "Movies" and dune.library_section_id == "lib-movies"
    # Naive local time, the form Plex's addedAt has, so the two sort together
    assert dune.added_at.tzinfo is None
    assert abs((datetime.now() - dune.added_at) - timedelta(days=1)) < timedelta(minutes=1)


@pytest.mark.parametrize("preference, chosen, size", [
    ("highest", "/data/movies/Dune/Dune 2160p.mkv", 60_000),
    ("lowest", "/data/movies/Dune/Dune 1080p.mkv", 12_000),
    ("1080p", "/data/movies/Dune/Dune 1080p.mkv", 12_000),
    ("4k", "/data/movies/Dune/Dune 2160p.mkv", 60_000),
])
def test_one_version_per_item_by_the_pin_rule(preference, chosen, size):
    result = _manager(FakeJellyfin()).get_recently_added_media(7, 50, version_preference=preference)
    dune = next(i for i in result.items if i.title == "Dune")
    assert (dune.file_path, dune.size) == (chosen, size)
    assert sorted((f["label"], f["is_preferred"]) for f in dune.version_files) == sorted(
        [("1080p", chosen.endswith("1080p.mkv")), ("4k", chosen.endswith("2160p.mkv"))])


def test_a_library_that_fails_is_named():
    result = _manager(FakeJellyfin(fail_library="lib-anime")).get_recently_added_media(7, 50)
    assert [i.title for i in result.items] == ["Dune"]
    assert result.unreadable_libraries == ["Anime"]


def test_a_switched_off_library_is_not_asked():
    from core.config import JellyfinConfig, PathMapping
    table, skipped = jellyfin_path_table(JellyfinConfig(), [
        PathMapping(name="Movies", plex_path="/data/movies/", real_path="/mnt/user/movies/",
                    jellyfin_path="/media/movies"),
        PathMapping(name="Anime", plex_path="", real_path="/mnt/user/anime/",
                    jellyfin_path="/media/anime", jellyfin_enabled=False)])
    fake = FakeJellyfin()
    JellyfinManager("http://jf:8096", "key", session=MagicMock(headers={}, get=MagicMock(side_effect=fake.get)),
                    path_mappings=table, skip_prefixes=skipped).get_recently_added_media(7, 50)
    assert [p["parentId"] for path, p in fake.calls if path == "/Items"] == ["lib-movies"]


def test_a_library_above_a_mapped_folder_is_asked():
    fake = FakeJellyfin()
    _manager(fake, {"/media/movies/Dune": "/data/dune"}).get_recently_added_media(7, 50)
    assert "lib-movies" in [p["parentId"] for path, p in fake.calls if path == "/Items"]


# --- RecentlyAddedService: both servers ------------------------------------

SETTINGS = {
    "number_episodes": 5, "valid_sections": [1], "days_to_monitor": 7, "users_toggle": False,
    "watchlist_toggle": False, "watchlist_episodes": 3, "watched_move": True, "cache_dir": "/mnt/cache/",
    "max_concurrent_moves_array": 2, "max_concurrent_moves_cache": 2, "prefetch_minimum_minutes": 0,
    "path_mappings": [
        {"name": "Movies", "plex_path": "/data/movies/", "real_path": "/mnt/user/movies/",
         "cache_path": "/mnt/cache/movies/", "enabled": True, "cacheable": True, "section_id": 1,
         "jellyfin_path": "/media/movies"},
        {"name": "Anime", "plex_path": "", "real_path": "/mnt/user/anime/", "cache_path": "/mnt/cache/anime/",
         "enabled": True, "cacheable": True, "jellyfin_path": "/media/anime"},
    ],
}
PLEX = {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "tok"}
JELLYFIN = {"jellyfin_enabled": True, "jellyfin_url": "http://jf:8096", "jellyfin_api_key": "key"}


def _item(path, title, hours_ago, source="plex", key="1"):
    return RecentlyAddedItem(file_path=path, rating_key=key, title=title, media_type="movie",
                             added_at=datetime.now() - timedelta(hours=hours_ago), library_title="Movies",
                             size=10, source=source)


@pytest.fixture
def run_service(tmp_path, monkeypatch):
    for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    def run(settings, plex_result=None, jellyfin_result=None, plex_error=None, jellyfin_error=None):
        from web.services.recently_added_service import RecentlyAddedService
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({**SETTINGS, **settings}), encoding="utf-8")
        svc = RecentlyAddedService()
        svc.settings_file = str(path)
        svc._file_exists = staticmethod(lambda p: False)
        svc._pin_path_map = staticmethod(lambda pm: {})
        svc._exclude_paths = staticmethod(lambda: set())
        svc._released_paths = staticmethod(lambda: set())
        plex = MagicMock()
        if plex_error:
            plex.get_recently_added_media.side_effect = plex_error
        else:
            plex.get_recently_added_media.return_value = plex_result or RecentlyAddedResult()
        jellyfin = MagicMock()
        if jellyfin_error:
            jellyfin.get_recently_added_media.side_effect = jellyfin_error
        else:
            jellyfin.get_recently_added_media.return_value = jellyfin_result or RecentlyAddedResult()
        with patch("core.plex_api.PlexManager", return_value=plex), \
                patch("core.jellyfin_api.JellyfinManager", return_value=jellyfin) as built:
            result = svc.get_recently_added(days=7, max_items=100)
        return result, plex, jellyfin, built

    return run


def test_jellyfin_only_lists_jellyfins_items(run_service):
    result, plex, _, built = run_service({**JELLYFIN, "plex_enabled": False}, jellyfin_result=RecentlyAddedResult(
        items=[_item("/mnt/user/anime/Frieren/S01E01.mkv", "Pilot", 1, "jellyfin", "c" * 32)]))
    assert result["available"] and [r.title for r in result["rows"]] == ["Pilot"]
    assert result["rows"][0].source == "jellyfin" and result["rows"][0].library_title == "Movies"
    plex.get_recently_added_media.assert_not_called()
    # Built from config with the run's path table: Jellyfin's folder -> the mapping's plex_path or real_path
    assert built.call_args.kwargs["path_mappings"] == {"/media/movies": "/data/movies/",
                                                       "/media/anime": "/mnt/user/anime/"}


def test_both_servers_merge_plex_first(run_service):
    plex_items = [_item("/data/movies/Dune.mkv", "Dune", 5)]
    jellyfin_items = [_item("/data/movies/Dune.mkv", "Dune", 4, "jellyfin", "a" * 32),  # same file
                      _item("/mnt/user/anime/S01E01.mkv", "Pilot", 1, "jellyfin", "c" * 32)]
    result, *_ = run_service({**PLEX, **JELLYFIN}, RecentlyAddedResult(items=plex_items),
                             RecentlyAddedResult(items=jellyfin_items, unreadable_libraries=["Anime"]))
    rows = result["rows"]
    assert [(r.title, r.source) for r in rows] == [("Pilot", "jellyfin"), ("Dune", "plex")]
    assert rows[0].library_title == "Movies (Jellyfin)" and rows[1].library_title == "Movies"
    assert result["unreadable_libraries"] == ["Anime (Jellyfin)"]


def test_a_failing_server_is_named_and_the_other_still_shows(run_service):
    result, *_ = run_service({**PLEX, **JELLYFIN}, RecentlyAddedResult(items=[_item("/data/movies/Dune.mkv", "Dune", 5)]),
                             jellyfin_error=ConnectionError("refused"))
    assert result["available"] and [r.title for r in result["rows"]] == ["Dune"]
    assert result["unreadable_libraries"] == ["Jellyfin"]

    result, *_ = run_service({**PLEX, **JELLYFIN}, plex_error=ConnectionError("refused"),
                             jellyfin_result=RecentlyAddedResult(items=[_item("/data/movies/Dune.mkv", "Dune", 5,
                                                                              "jellyfin", "a" * 32)]))
    assert [r.source for r in result["rows"]] == ["jellyfin"] and result["unreadable_libraries"] == ["Plex"]


def test_both_failing_is_an_error(run_service):
    result, *_ = run_service({**JELLYFIN, "plex_enabled": False}, jellyfin_error=ConnectionError("refused"))
    assert not result["available"] and "Jellyfin" in result["error"]


def test_plex_only_is_unchanged(run_service):
    result, plex, jellyfin, built = run_service(PLEX, RecentlyAddedResult(items=[_item("/data/movies/Dune.mkv", "Dune", 5)]))
    assert [r.title for r in result["rows"]] == ["Dune"] and result["rows"][0].library_title == "Movies"
    built.assert_not_called()


def test_jellyfin_rows_disable_the_pin_button_for_now():
    from pathlib import Path
    from jinja2 import Environment, FileSystemLoader
    templates = Path(__file__).resolve().parent.parent / "web" / "templates"
    source = (templates / "recently_added" / "partials" / "list.html").read_text(encoding="utf-8")
    assert "{% if row.source != 'plex' %}" in source
    Environment(loader=FileSystemLoader(str(templates))).get_template("recently_added/partials/list.html")
