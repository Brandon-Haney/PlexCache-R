"""The duplicate scan on Jellyfin (docs/media-servers-design.md, question 6).

Plex merges copies of a film or episode into one item, so its scan looks for
items with more than one file. Jellyfin merges only files named as versions
(one item, MediaSourceCount > 1) and lists other copies as items of their own.
The Jellyfin scan counts both: it lists each mapped library a page at a time
with ProviderIds and MediaSourceCount only, groups items by the key a file swap
keeps (a movie's TMDb/IMDb id; an episode's series, season and episode), and
fetches files only for the groups with more than one. The results go through
the same multi-version check, Sonarr/Radarr orphan check and delete step as
Plex's.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault('fcntl', MagicMock())
for _mod in ['apscheduler', 'apscheduler.schedulers', 'apscheduler.schedulers.background',
             'apscheduler.triggers', 'apscheduler.triggers.cron', 'apscheduler.triggers.interval',
             'plexapi', 'plexapi.server']:
    sys.modules.setdefault(_mod, MagicMock())

from core.jellyfin_api import JellyfinManager  # noqa: E402

SHOGUN = "5" * 32


def _video(width, height, codec="hevc"):
    return [{"Type": "Video", "Width": width, "Height": height, "Codec": codec}, {"Type": "Audio"}]


def _source(path, size, width=1920, height=800):
    return {"Path": path, "Size": size, "Container": "mkv", "Bitrate": 8_000_000, "MediaStreams": _video(width, height)}


def _movie(item_id, name, sources, tmdb=None, year=2021):
    item = {"Id": item_id, "Name": name, "Type": "Movie", "ProductionYear": year, "MediaSources": sources,
            "ProviderIds": {"Tmdb": tmdb} if tmdb else {}}
    if len(sources) > 1:
        item["MediaSourceCount"] = len(sources)
    return item


def _episode(item_id, number, path):
    return {"Id": item_id, "Name": f"Chapter {number}", "Type": "Episode", "SeriesId": SHOGUN, "SeriesName": "Shogun",
            "ParentIndexNumber": 1, "IndexNumber": number, "MediaSources": [_source(path, 5)]}


MOVIES = [
    # Two copies Radarr left behind: separate items, same TMDb id
    _movie("d1" * 16, "Dune", [_source("/media/movies/Dune (2021)/Dune 1080p.mkv", 10)], tmdb="438631"),
    _movie("d2" * 16, "Dune", [_source("/media/movies/Dune (2021)/Dune Remux-1080p.mkv", 40)], tmdb="438631"),
    # Named as versions: one item, two files (4K + 1080p, kept on purpose)
    _movie("a1" * 16, "Arrival", [_source("/media/movies/Arrival (2016)/Arrival - 1080p.mkv", 9),
                                  _source("/media/movies/Arrival (2016)/Arrival - 2160p.mkv", 50, 3840, 1600)],
           tmdb="329865", year=2016),
    _movie("h1" * 16, "Heat", [_source("/media/movies/Heat (1995)/Heat.mkv", 8)], tmdb="949", year=1995),
    # Not identified: nothing to group by, so never grouped with each other
    _movie("x1" * 16, "Home Video", [_source("/media/movies/Home/a.mkv", 1)]),
    _movie("x2" * 16, "Home Video", [_source("/media/movies/Home/b.mkv", 1)]),
]
EPISODES = [_episode("e1" * 16, 1, "/media/tv/Shogun/Season 1/S01E01.mkv"),
            _episode("e2" * 16, 1, "/media/tv/Shogun/Season 1/S01E01 PROPER.mkv"),
            _episode("e3" * 16, 2, "/media/tv/Shogun/Season 1/S01E02.mkv")]
OTHER = [_movie("o1" * 16, "Dune", [_source("/media/other/Dune.mkv", 10)], tmdb="438631"),
         _movie("o2" * 16, "Dune", [_source("/media/other/Dune 2.mkv", 10)], tmdb="438631")]
LIBRARIES = [
    {"Name": "Movies", "ItemId": "lib-movies", "CollectionType": "movies", "Locations": ["/media/movies"]},
    {"Name": "Shows", "ItemId": "lib-tv", "CollectionType": "tvshows", "Locations": ["/media/tv"]},
    {"Name": "Other", "ItemId": "lib-other", "CollectionType": "movies", "Locations": ["/media/other"]},
]
BY_LIBRARY = {"lib-movies": MOVIES, "lib-tv": EPISODES, "lib-other": OTHER}
EVERYTHING = {item["Id"]: item for items in BY_LIBRARY.values() for item in items}


class FakeJellyfin:
    """Library listings (paged, without files) and items by id (with files)."""

    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        path, params = url.split("8096", 1)[1], dict(params or {})
        self.calls.append((path, params))
        response = MagicMock()
        response.raise_for_status.return_value = None
        if path == "/Library/VirtualFolders":
            response.json.return_value = LIBRARIES
        elif path == "/Items" and "ids" in params:
            assert params["fields"] == "Path,MediaSources"
            response.json.return_value = {"Items": [EVERYTHING[i] for i in params["ids"].split(",")]}
        elif path == "/Items" and "parentId" in params:
            assert params["fields"] == "ProviderIds,MediaSourceCount"
            assert params["enableImages"] == "false" and params["enableUserData"] == "false"
            types = params["includeItemTypes"].split(",")
            listed = [{k: v for k, v in item.items() if k != "MediaSources"}
                      for item in BY_LIBRARY[params["parentId"]] if item["Type"] in types]
            start, limit = int(params["startIndex"]), int(params["limit"])
            response.json.return_value = {"Items": listed[start:start + limit], "TotalRecordCount": len(listed)}
        else:
            raise AssertionError(f"unexpected {path} {params}")
        return response


def _manager(fake):
    # "Other" has no mapping, as a library switched off on the Libraries tab
    return JellyfinManager("http://jf:8096", "key", session=fake,
                           path_mappings={"/media/movies": "/data/movies", "/media/tv": "/mnt/user/tv"})


def test_copies_and_versions_are_grouped_by_what_jellyfin_keeps():
    fake = FakeJellyfin()
    jellyfin = _manager(fake)
    jellyfin.LISTING_PAGE = 2  # every page is asked for
    groups, libraries, scanned = jellyfin.duplicate_groups()
    assert libraries == ["Movies", "Shows"] and scanned == 9
    assert [(g["key"], g["title"], g["item_type"], g["library"], len(g["items"])) for g in groups] == [
        ("jellyfin:movie:tmdb:438631", "Dune (2021)", "movie", "Movies", 2),
        ("jellyfin:movie:tmdb:329865", "Arrival (2016)", "movie", "Movies", 1),
        (f"jellyfin:episode:{SHOGUN}:1:1", "Shogun - S01E01 - Chapter 1", "episode", "Shows", 2),
    ]
    # Files are fetched for those groups only, never for the whole library
    fetched = [i for path, params in fake.calls if "ids" in params for i in params["ids"].split(",")]
    assert sorted(fetched) == sorted(["d1" * 16, "d2" * 16, "a1" * 16, "e1" * 16, "e2" * 16])
    assert not any(params.get("parentId") == "lib-other" for _, params in fake.calls)


# --- DuplicateService.scan_libraries ------------------------------------------------------------

BASE = {"number_episodes": 2, "days_to_monitor": 7, "watchlist_toggle": False, "watchlist_episodes": 1,
        "watched_move": True, "max_concurrent_moves_array": 1, "max_concurrent_moves_cache": 1,
        "cache_dir": "/mnt/cache/", "jellyfin_url": "http://jf:8096", "jellyfin_api_key": "key",
        "jellyfin_enabled": True,
        "path_mappings": [
            {"name": "Movies", "plex_path": "/data/movies/", "real_path": "/mnt/user/movies/",
             "cache_path": "/mnt/cache/movies/", "cacheable": True, "enabled": True,
             "jellyfin_path": "/media/movies", "jellyfin_enabled": True},
            {"name": "TV", "plex_path": "", "real_path": "/mnt/user/tv/", "cache_path": "/mnt/cache/tv/",
             "cacheable": True, "enabled": True, "jellyfin_path": "/media/tv", "jellyfin_enabled": True},
        ]}


@pytest.fixture
def scan(tmp_path, monkeypatch):
    for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_PATH_MAPPINGS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)

    def scan(settings, plex=None):
        from web.services import duplicate_service
        settings_file = tmp_path / "plexcache_settings.json"
        settings_file.write_text(json.dumps({**BASE, **settings}), encoding="utf-8")
        service = duplicate_service.DuplicateService()
        with patch("web.config.SETTINGS_FILE", settings_file), \
                patch.object(duplicate_service, "SCAN_RESULTS_FILE", str(tmp_path / "scan.json")), \
                patch.object(duplicate_service, "IGNORE_FILE", str(tmp_path / "ignores.json")), \
                patch("core.jellyfin_api.requests.Session", return_value=FakeJellyfin()), \
                patch("plexapi.server.PlexServer", return_value=plex):
            result = service.scan_libraries()
            return result, service.load_scan_results()

    return scan


def test_a_jellyfin_only_install_is_scanned(scan):
    result, results = scan({"plex_enabled": False})
    assert result.success, result.message
    assert results.libraries_scanned == ["Movies", "Shows"] and results.total_items == 9
    by_key = {item.rating_key: item for item in results.items}
    dune = by_key["jellyfin:movie:tmdb:438631"]
    assert [(f.file_path, f.fs_path, f.size, f.resolution) for f in dune.files] == [
        ("/media/movies/Dune (2021)/Dune 1080p.mkv", "/mnt/user/movies/Dune (2021)/Dune 1080p.mkv", 10, "1080"),
        ("/media/movies/Dune (2021)/Dune Remux-1080p.mkv", "/mnt/user/movies/Dune (2021)/Dune Remux-1080p.mkv",
         40, "1080")]
    assert (dune.files[0].container, dune.files[0].video_codec, dune.files[0].bitrate) == ("mkv", "hevc", 8000)
    assert not dune.is_multi_version
    # 4K beside 1080p is kept on purpose, as on Plex
    assert by_key["jellyfin:movie:tmdb:329865"].is_multi_version
    shogun = by_key[f"jellyfin:episode:{SHOGUN}:1:1"]
    assert shogun.files[1].fs_path == "/mnt/user/tv/Shogun/Season 1/S01E01 PROPER.mkv"  # Jellyfin-only library
    assert (results.duplicate_count, results.multi_version_count) == (2, 1)


def test_a_group_with_a_file_no_mapping_covers_is_left_out(scan):
    """Its file couldn't be offered for deletion by a path PlexCache-D knows."""
    stray = _source("/elsewhere/Dune.mkv", 10)
    MOVIES[1]["MediaSources"].append(stray)
    try:
        _, results = scan({"plex_enabled": False})
    finally:
        MOVIES[1]["MediaSources"].remove(stray)
    assert "jellyfin:movie:tmdb:438631" not in {item.rating_key for item in results.items}


def _plex_with_dune():
    """Plex has the same two Dune files, merged into one item as Plex does."""
    def media(path, size):
        part = MagicMock(file=path, size=size)
        return MagicMock(parts=[part], videoResolution="1080", container="mkv", videoCodec="hevc", bitrate=8000)
    movie = MagicMock(title="Dune", year=2021, ratingKey=101,
                      media=[media("/data/movies/Dune (2021)/Dune 1080p.mkv", 10),
                             media("/data/movies/Dune (2021)/Dune Remux-1080p.mkv", 40)])
    section = MagicMock(type="movie", title="Movies")
    section.all.return_value = [movie]
    plex = MagicMock(friendlyName="Plex")
    plex.library.sections.return_value = [section]
    return plex


def test_with_both_servers_jellyfin_adds_only_what_plex_didnt_list(scan):
    result, results = scan({"plex_enabled": True, "PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "tok",
                            "valid_sections": [1], "users_toggle": False},
                           plex=_plex_with_dune())
    assert result.success, result.message
    assert results.libraries_scanned == ["Movies", "Movies (Jellyfin)", "Shows (Jellyfin)"]
    assert [(item.rating_key, item.library) for item in results.items] == [
        ("101", "Movies"),  # Plex's Dune; Jellyfin's copy of it is left out
        ("jellyfin:movie:tmdb:329865", "Movies (Jellyfin)"),
        (f"jellyfin:episode:{SHOGUN}:1:1", "Shows (Jellyfin)"),
    ]
