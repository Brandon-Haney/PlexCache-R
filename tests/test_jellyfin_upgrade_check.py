"""Following a Sonarr/Radarr file swap on Jellyfin (docs/media-servers-design.md, question 5).

Plex keeps an item's rating key when its file is replaced; Jellyfin derives an
item's id from its path, so the new file is a new item. What Jellyfin keeps is
what it keys watch progress by (BaseItem.GetUserDataKeys): a movie's TMDb or
IMDb id, an episode's series with its season and episode numbers. PlexCache-D
records that as the OnDeck entry's jellyfin_key, beside Plex's rating_key, and
matches on it in the run (the next run sees the new file under the same key)
and in the Maintenance audit (asks Jellyfin which file answers to the key now).
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.modules.setdefault('fcntl', MagicMock())
for _mod in ['apscheduler', 'apscheduler.schedulers', 'apscheduler.schedulers.background',
             'apscheduler.triggers', 'apscheduler.triggers.cron', 'apscheduler.triggers.interval',
             'plexapi', 'plexapi.server']:
    sys.modules.setdefault(_mod, MagicMock())

from core.file_operations import CacheTimestampTracker, OnDeckTracker, WatchlistTracker  # noqa: E402
from core.jellyfin_api import JellyfinManager, upgrade_key  # noqa: E402
from core.media_server import OnDeckItem  # noqa: E402

SERIES = "5" * 32
DUNE_KEY = "jellyfin:movie:tmdb:438631"
EPISODE_KEY = f"jellyfin:episode:{SERIES}:1:2"


def _movie(item_id, path, **extra):
    return {"Id": item_id, "Name": "Dune", "Type": "Movie", "ProviderIds": {"Tmdb": "438631", "Imdb": "tt1160419"},
            "MediaSources": [{"Path": path, "Size": 10}], **extra}


def _episode(item_id, number, path):
    return {"Id": item_id, "Name": f"Chapter {number}", "Type": "Episode", "SeriesId": SERIES,
            "SeriesName": "Shogun", "ParentIndexNumber": 1, "IndexNumber": number,
            "MediaSources": [{"Path": path, "Size": 10}]}


# --- The key -------------------------------------------------------------------------

def test_the_key_is_what_jellyfin_keeps_across_a_swap():
    assert upgrade_key(_movie("a", "/x.mkv")) == DUNE_KEY
    assert upgrade_key({"Type": "Movie", "ProviderIds": {"imdb": "tt1160419"}}) == "jellyfin:movie:imdb:tt1160419"
    assert upgrade_key(_episode("e", 2, "/x.mkv")) == EPISODE_KEY
    # Not identified yet: nothing to match on, as with a Plex entry without a rating key
    assert upgrade_key({"Type": "Movie", "ProviderIds": {}}) is None
    assert upgrade_key({"Type": "Episode", "SeriesId": SERIES, "IndexNumber": 2}) is None
    assert upgrade_key({"Type": "Series", "ProviderIds": {"Tvdb": "1"}}) is None


# --- The OnDeck tracker -----------------------------------------------------------------

def test_the_tracker_keeps_each_servers_key(tmp_path):
    tracker = OnDeckTracker(str(tmp_path / "ondeck.json"))
    tracker.update_entry("/mnt/user/movies/Dune.mkv", "anna", rating_key="101")
    tracker.update_entry("/mnt/user/movies/Dune.mkv", "roger", source="jellyfin", jellyfin_key=DUNE_KEY)
    entry = tracker.get_entry("/mnt/user/movies/Dune.mkv")
    assert (entry["rating_key"], entry["jellyfin_key"]) == ("101", DUNE_KEY)
    assert tracker.find_by_rating_key("101") == tracker.find_by_rating_key(DUNE_KEY) == {"/mnt/user/movies/Dune.mkv"}

    reloaded = OnDeckTracker(str(tmp_path / "ondeck.json"))
    assert reloaded.find_by_rating_key(DUNE_KEY) == {"/mnt/user/movies/Dune.mkv"}
    reloaded.remove_entry("/mnt/user/movies/Dune.mkv")
    assert reloaded.find_by_rating_key("101") is None and reloaded.find_by_rating_key(DUNE_KEY) is None


def test_a_plex_only_entry_is_written_as_before(tmp_path):
    tracker = OnDeckTracker(str(tmp_path / "ondeck.json"))
    tracker.update_entry("/mnt/user/movies/Dune.mkv", "anna", rating_key="101")
    assert "jellyfin_key" not in json.loads((tmp_path / "ondeck.json").read_text())["/mnt/user/movies/Dune.mkv"]


# --- The run --------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    from core.app import PlexCacheApp
    with patch.object(PlexCacheApp, '__init__', lambda self, *a, **kw: None):
        app = PlexCacheApp.__new__(PlexCacheApp)
    app.dry_run = False
    app.ondeck_tracker = OnDeckTracker(str(tmp_path / "ondeck.json"))
    app.watchlist_tracker = WatchlistTracker(str(tmp_path / "watchlist.json"))
    app.timestamp_tracker = CacheTimestampTracker(str(tmp_path / "timestamps.json"))
    app.file_filter = MagicMock()
    app.file_path_modifier = MagicMock()
    app.file_path_modifier.convert_real_to_cache.side_effect = lambda p: (p.replace("/mnt/user/", "/mnt/cache/"), None)
    app.config_manager = MagicMock()
    app.config_manager.cache.create_plexcached_backups = False
    return app


OLD, NEW = "/mnt/user/movies/Dune (2021)/Dune 1080p.mkv", "/mnt/user/movies/Dune (2021)/Dune 2160p.mkv"


def test_a_jellyfin_swap_moves_the_tracking_to_the_new_file(app):
    app.ondeck_tracker.update_entry(OLD, "roger", source="jellyfin", jellyfin_key=DUNE_KEY)
    app.timestamp_tracker.record_cache_time(OLD.replace("/mnt/user/", "/mnt/cache/"), source="ondeck")
    pre_run = {k: set(v) for k, v in app.ondeck_tracker._rating_key_index.items()}
    item = OnDeckItem(NEW, "roger", source="jellyfin", jellyfin_key=DUNE_KEY)
    app.ondeck_tracker.update_entry(NEW, "roger", source="jellyfin", jellyfin_key=DUNE_KEY)

    app._detect_and_transfer_upgrades([item], {NEW: NEW}, pre_run)

    assert app.ondeck_tracker.get_entry(OLD) is None
    app.file_filter.remove_files_from_exclude_list.assert_called_once_with([OLD.replace("/mnt/user/", "/mnt/cache/")])
    app.file_filter._add_to_exclude_file.assert_called_once_with(NEW.replace("/mnt/user/", "/mnt/cache/"))
    new_entry = app.timestamp_tracker.get_entry(NEW.replace("/mnt/user/", "/mnt/cache/"))
    assert new_entry["source"] == "ondeck" and "rating_key" not in new_entry  # no Plex key to store


def test_a_file_both_servers_follow_is_moved_once(app):
    """Plex finds the swap under its rating key and Jellyfin under its own: one transfer,
    so the second doesn't find the timestamp gone and record the source as unknown."""
    app.ondeck_tracker.update_entry(OLD, "anna", rating_key="101")
    app.ondeck_tracker.update_entry(OLD, "roger", source="jellyfin", jellyfin_key=DUNE_KEY)
    app.timestamp_tracker.record_cache_time(OLD.replace("/mnt/user/", "/mnt/cache/"), source="ondeck")
    pre_run = {k: set(v) for k, v in app.ondeck_tracker._rating_key_index.items()}
    items = [OnDeckItem(NEW, "anna", rating_key="101"),
             OnDeckItem(NEW, "roger", source="jellyfin", jellyfin_key=DUNE_KEY)]

    app._detect_and_transfer_upgrades(items, {NEW: NEW}, pre_run)

    assert app.file_filter._add_to_exclude_file.call_count == 1
    assert app.timestamp_tracker.get_entry(NEW.replace("/mnt/user/", "/mnt/cache/"))["source"] == "ondeck"


# --- Asking Jellyfin which file answers to a key ------------------------------------------

LIBRARIES = [{"Name": "Movies", "ItemId": "lib-movies", "CollectionType": "movies", "Locations": ["/media/movies"]},
             {"Name": "Shows", "ItemId": "lib-tv", "CollectionType": "tvshows", "Locations": ["/media/tv"]}]


class FakeJellyfin:
    """The requests a lookup makes: libraries, a movie library listing (paged), items by
    id, and a series' episodes by season number, as Jellyfin answers without a user."""

    def __init__(self, movies=(), episodes=()):
        self.movies, self.episodes = list(movies), list(episodes)
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        path, params = url.split("8096", 1)[1], dict(params or {})
        self.calls.append((path, params))
        response = MagicMock(status_code=200)
        response.raise_for_status.return_value = None
        if path == "/Library/VirtualFolders":
            response.json.return_value = LIBRARIES
        elif path == "/Items" and "ids" in params:
            wanted = params["ids"].split(",")
            response.json.return_value = {"Items": [m for m in self.movies if m["Id"] in wanted]}
        elif path == "/Items" and params.get("parentId") == "lib-movies":
            assert params["fields"] == "ProviderIds" and params["enableImages"] == "false"
            listed = [{k: v for k, v in m.items() if k != "MediaSources"} for m in self.movies]
            start, limit = int(params["startIndex"]), int(params["limit"])
            response.json.return_value = {"Items": listed[start:start + limit], "TotalRecordCount": len(listed)}
        elif path == f"/Shows/{SERIES}/Episodes":
            response.json.return_value = {"Items": [e for e in self.episodes
                                                    if str(e["ParentIndexNumber"]) == str(params["season"])]}
        elif path.startswith("/Shows/"):
            response.status_code = 404
            response.raise_for_status.side_effect = requests.HTTPError("404", response=response)
        else:
            raise AssertionError(f"unexpected {path} {params}")
        return response


def _manager(fake):
    return JellyfinManager("http://jf:8096", "key", session=fake,
                           path_mappings={"/media/movies": "/mnt/user/movies", "/media/tv": "/data/tv"})


def test_a_movie_key_finds_the_replacement_in_the_library():
    fake = FakeJellyfin(movies=[_movie("n" * 32, "/media/movies/Dune (2021)/Dune 2160p.mkv"),
                                {**_movie("h" * 32, "/media/movies/Heat/Heat.mkv"), "ProviderIds": {"Tmdb": "949"}}])
    jellyfin = _manager(fake)
    jellyfin.LISTING_PAGE = 1  # every page is asked for
    assert jellyfin.files_for_upgrade_key(DUNE_KEY) == [NEW]
    assert jellyfin.files_for_upgrade_key("jellyfin:movie:tmdb:1") == []
    listings = [c for c in fake.calls if c[1].get("parentId")]
    assert len(listings) == 2  # read once, then reused for the second key


def test_an_episode_key_asks_its_series_for_that_season():
    fake = FakeJellyfin(episodes=[_episode("1" * 32, 1, "/media/tv/Shogun/S01E01.mkv"),
                                  _episode("2" * 32, 2, "/media/tv/Shogun/S01E02 2160p.mkv")])
    jellyfin = _manager(fake)
    assert jellyfin.files_for_upgrade_key(EPISODE_KEY) == ["/data/tv/Shogun/S01E02 2160p.mkv"]
    assert fake.calls[-1][1]["season"] == "1"
    assert jellyfin.files_for_upgrade_key(f"jellyfin:episode:{'9' * 32}:1:2") == []  # series gone: 404


# --- The Maintenance audit (CacheService.check_for_upgrades) ---------------------------------

@pytest.fixture
def audit(tmp_path, monkeypatch):
    """A Jellyfin-only install with Dune 1080p cached and on roger's Continue Watching,
    then replaced by Radarr: the new file is on the cache, the old one is gone."""
    for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_PATH_MAPPINGS"):
        monkeypatch.delenv(key, raising=False)
    cache = tmp_path.as_posix() + "/cache/movies/"
    settings = {"number_episodes": 2, "days_to_monitor": 7, "watchlist_toggle": False, "watchlist_episodes": 1,
                "watched_move": True, "max_concurrent_moves_array": 1, "max_concurrent_moves_cache": 1,
                "plex_enabled": False, "jellyfin_enabled": True, "jellyfin_url": "http://jf:8096",
                "jellyfin_api_key": "key", "create_plexcached_backups": False, "cache_dir": tmp_path.as_posix(),
                "path_mappings": [{"name": "Movies", "plex_path": "", "real_path": "/mnt/user/movies/",
                                   "cache_path": cache, "cacheable": True, "enabled": True,
                                   "jellyfin_path": "/media/movies", "jellyfin_enabled": True}]}
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    old_cache, new_cache = cache + "Dune (2021)/Dune 1080p.mkv", cache + "Dune (2021)/Dune 2160p.mkv"
    (tmp_path / "cache/movies/Dune (2021)").mkdir(parents=True)
    (tmp_path / "cache/movies/Dune (2021)/Dune 2160p.mkv").write_bytes(b"x")
    (tmp_path / "ondeck.json").write_text(json.dumps({OLD: {
        "users": ["roger"], "sources": ["jellyfin"], "jellyfin_key": DUNE_KEY}}), encoding="utf-8")
    (tmp_path / "timestamps.json").write_text(json.dumps({old_cache: {
        "cached_at": "2026-11-01T10:00:00", "source": "ondeck"}}), encoding="utf-8")
    (tmp_path / "exclude.txt").write_text(old_cache + "\n", encoding="utf-8")

    from web.services.cache_service import CacheService
    service = CacheService.__new__(CacheService)
    service.settings_file = tmp_path / "settings.json"
    service.ondeck_file = tmp_path / "ondeck.json"
    service.timestamps_file = tmp_path / "timestamps.json"
    service.watchlist_file = tmp_path / "watchlist.json"
    service.exclude_file = tmp_path / "exclude.txt"

    def check(fake):
        with patch("core.jellyfin_api.requests.Session", return_value=fake):
            return service.check_for_upgrades([old_cache])

    return check, service, old_cache, new_cache, tmp_path


def test_the_audit_follows_a_jellyfin_swap(audit):
    check, service, old_cache, new_cache, tmp_path = audit
    result = check(FakeJellyfin(movies=[_movie("n" * 32, "/media/movies/Dune (2021)/Dune 2160p.mkv")]))
    assert (result["upgrades_found"], result["upgrades_resolved"]) == (1, 1)
    assert result["details"][0]["rating_key"] == DUNE_KEY
    assert OLD not in service.get_ondeck_tracker()
    assert service.get_timestamps()[new_cache]["source"] == "ondeck" and old_cache not in service.get_timestamps()
    assert (tmp_path / "exclude.txt").read_text(encoding="utf-8").splitlines() == [new_cache]


def test_the_audit_leaves_a_file_that_still_answers(audit):
    check, service, old_cache, _, _ = audit
    result = check(FakeJellyfin(movies=[_movie("o" * 32, "/media/movies/Dune (2021)/Dune 1080p.mkv"),
                                        _movie("n" * 32, "/media/movies/Dune (2021)/Dune 2160p.mkv")]))
    assert result["upgrades_found"] == 0 and OLD in service.get_ondeck_tracker()


def test_with_two_copies_the_audit_picks_the_untracked_one(audit):
    """Dune kept in two copies: the extended cut is already tracked, so the
    swapped 1080p can only have become the 2160p file."""
    check, service, old_cache, new_cache, tmp_path = audit
    extended = "/mnt/user/movies/Dune (2021)/Dune Extended.mkv"
    data = json.loads((tmp_path / "ondeck.json").read_text())
    data[extended] = {"users": ["mia"], "sources": ["jellyfin"], "jellyfin_key": DUNE_KEY}
    (tmp_path / "ondeck.json").write_text(json.dumps(data), encoding="utf-8")
    result = check(FakeJellyfin(movies=[_movie("e" * 32, "/media/movies/Dune (2021)/Dune Extended.mkv"),
                                        _movie("n" * 32, "/media/movies/Dune (2021)/Dune 2160p.mkv")]))
    assert result["upgrades_resolved"] == 1 and result["details"][0]["new_file"] == "Dune 2160p.mkv"


def test_an_unreachable_jellyfin_changes_nothing(audit):
    check, service, old_cache, _, _ = audit
    fake = FakeJellyfin()
    fake.get = MagicMock(side_effect=requests.ConnectionError("refused"))
    result = check(fake)
    assert result == {"upgrades_found": 0, "upgrades_resolved": 0, "details": []}
    assert OLD in service.get_ondeck_tracker()


def test_plexs_answer_stands_for_a_file_both_servers_report(audit):
    check, service, old_cache, new_cache, tmp_path = audit
    settings = json.loads((tmp_path / "settings.json").read_text())
    settings.update({"plex_enabled": True, "PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "tok"})
    settings["path_mappings"][0]["plex_path"] = "/data/movies/"
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    data = json.loads((tmp_path / "ondeck.json").read_text())
    data[OLD]["rating_key"] = "101"
    (tmp_path / "ondeck.json").write_text(json.dumps(data), encoding="utf-8")
    plex = MagicMock()
    plex.fetchItem.return_value.media[0].parts[0].file = "/data/movies/Dune (2021)/Dune 2160p.mkv"
    fake = FakeJellyfin()
    with patch("plexapi.server.PlexServer", return_value=plex):
        result = check(fake)
    assert result["upgrades_resolved"] == 1 and result["details"][0]["rating_key"] == "101"
    assert fake.calls == []  # Jellyfin wasn't asked
