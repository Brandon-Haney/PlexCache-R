"""Tests for extension-free caching (#6).

Tests SiblingFileFinder sibling discovery, CacheTimestampTracker generalization
(associate_files, migration, reference counting), priority delegation for
non-video files, and .plexcached three-way category matching.
"""

import os
import json
import tempfile
import shutil
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from core.file_operations import (
    SiblingFileFinder,
    SubtitleFinder,
    CacheTimestampTracker,
    CachePriorityManager,
    OnDeckTracker,
    WatchlistTracker,
    is_video_file,
    is_directory_level_file,
    _get_file_category,
    find_matching_plexcached,
    PLEXCACHED_EXTENSION,
)
from conftest import create_test_file


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="plexcache_efctest_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ============================================================
# is_video_file tests
# ============================================================

class TestIsVideoFile:
    def test_video_extensions(self):
        assert is_video_file("movie.mkv") is True
        assert is_video_file("movie.mp4") is True
        assert is_video_file("movie.avi") is True

    def test_non_video_extensions(self):
        assert is_video_file("movie.srt") is False
        assert is_video_file("poster.jpg") is False
        assert is_video_file("movie.nfo") is False

    def test_case_insensitive(self):
        assert is_video_file("movie.MKV") is True
        assert is_video_file("movie.Mp4") is True


# ============================================================
# is_directory_level_file tests
# ============================================================

class TestIsDirectoryLevelFile:
    def test_name_prefixed_file(self):
        """Files starting with video's base name are NOT directory-level."""
        assert is_directory_level_file(
            "/media/Movie (2020).nfo",
            "/media/Movie (2020).mkv"
        ) is False

    def test_name_prefixed_subtitle(self):
        assert is_directory_level_file(
            "/media/Movie (2020).en.srt",
            "/media/Movie (2020).mkv"
        ) is False

    def test_directory_level_poster(self):
        """poster.jpg is not prefixed with the video name."""
        assert is_directory_level_file(
            "/media/poster.jpg",
            "/media/Movie (2020).mkv"
        ) is True

    def test_directory_level_fanart(self):
        assert is_directory_level_file(
            "/media/fanart.jpg",
            "/media/Movie (2020).mkv"
        ) is True


# ============================================================
# _get_file_category tests
# ============================================================

class TestGetFileCategory:
    def test_video(self):
        assert _get_file_category("movie.mkv") == "video"
        assert _get_file_category("movie.mp4") == "video"

    def test_subtitle(self):
        assert _get_file_category("movie.srt") == "subtitle"
        assert _get_file_category("movie.ass") == "subtitle"

    def test_sidecar(self):
        assert _get_file_category("poster.jpg") == "sidecar"
        assert _get_file_category("movie.nfo") == "sidecar"
        assert _get_file_category("fanart.png") == "sidecar"


# ============================================================
# SiblingFileFinder tests
# ============================================================

class TestSiblingFileFinder:
    def test_finds_subtitles(self, temp_dir):
        """Sibling finder discovers subtitle files."""
        video = create_test_file(os.path.join(temp_dir, "Movie.mkv"), "video")
        sub = create_test_file(os.path.join(temp_dir, "Movie.en.srt"), "sub")
        finder = SiblingFileFinder()
        result = finder.get_media_siblings_grouped([video])
        assert sub in result[video]

    def test_finds_artwork(self, temp_dir):
        """Sibling finder discovers artwork files."""
        video = create_test_file(os.path.join(temp_dir, "Movie.mkv"), "video")
        poster = create_test_file(os.path.join(temp_dir, "poster.jpg"), "img")
        fanart = create_test_file(os.path.join(temp_dir, "fanart.jpg"), "img")
        finder = SiblingFileFinder()
        result = finder.get_media_siblings_grouped([video])
        assert poster in result[video]
        assert fanart in result[video]

    def test_finds_nfo(self, temp_dir):
        """Sibling finder discovers NFO files."""
        video = create_test_file(os.path.join(temp_dir, "Movie.mkv"), "video")
        nfo = create_test_file(os.path.join(temp_dir, "Movie.nfo"), "nfo")
        finder = SiblingFileFinder()
        result = finder.get_media_siblings_grouped([video])
        assert nfo in result[video]

    def test_skips_other_videos(self, temp_dir):
        """Sibling finder does NOT include other video files."""
        video1 = create_test_file(os.path.join(temp_dir, "Movie1.mkv"), "video1")
        create_test_file(os.path.join(temp_dir, "Movie2.mkv"), "video2")
        finder = SiblingFileFinder()
        result = finder.get_media_siblings_grouped([video1])
        # Movie2.mkv should not be in Movie1's siblings
        siblings = result[video1]
        for s in siblings:
            assert not s.endswith(".mkv")

    def test_skips_hidden_files(self, temp_dir):
        """Sibling finder skips hidden files (dotfiles)."""
        video = create_test_file(os.path.join(temp_dir, "Movie.mkv"), "video")
        create_test_file(os.path.join(temp_dir, ".hidden"), "hidden")
        finder = SiblingFileFinder()
        result = finder.get_media_siblings_grouped([video])
        for s in result[video]:
            assert not os.path.basename(s).startswith(".")

    def test_skips_plexcached_files(self, temp_dir):
        """Sibling finder skips .plexcached backup files."""
        video = create_test_file(os.path.join(temp_dir, "Movie.mkv"), "video")
        create_test_file(os.path.join(temp_dir, "Movie.mkv.plexcached"), "backup")
        create_test_file(os.path.join(temp_dir, "OtherMovie.mkv.plexcached"), "backup2")
        create_test_file(os.path.join(temp_dir, "poster.jpg"), "img")
        finder = SiblingFileFinder()
        result = finder.get_media_siblings_grouped([video])
        names = [os.path.basename(f) for f in result[video]]
        assert "poster.jpg" in names
        assert not any(n.endswith(".plexcached") for n in names)

    def test_backward_compat_alias(self):
        """SubtitleFinder is an alias for SiblingFileFinder."""
        assert SubtitleFinder is SiblingFileFinder

    def test_get_media_subtitles_grouped_filters_subtitles_only(self, temp_dir):
        """Backward-compat method only returns subtitle files."""
        video = create_test_file(os.path.join(temp_dir, "Movie.mkv"), "video")
        sub = create_test_file(os.path.join(temp_dir, "Movie.en.srt"), "sub")
        create_test_file(os.path.join(temp_dir, "poster.jpg"), "img")
        finder = SiblingFileFinder()
        result = finder.get_media_subtitles_grouped([video])
        assert sub in result[video]
        # poster.jpg should NOT be in subtitle-only results
        for s in result[video]:
            assert not s.endswith(".jpg")

    def test_empty_directory(self, temp_dir):
        """Video with no siblings returns empty list."""
        video = create_test_file(os.path.join(temp_dir, "Movie.mkv"), "video")
        finder = SiblingFileFinder()
        result = finder.get_media_siblings_grouped([video])
        assert result[video] == []


# ============================================================
# CacheTimestampTracker migration tests
# ============================================================

class TestTrackerMigration:
    def test_subtitles_key_migrated_to_associated_files(self, temp_dir):
        """Old 'subtitles' key is migrated to 'associated_files' on load."""
        ts_file = os.path.join(temp_dir, "timestamps.json")
        data = {
            "/cache/Movie.mkv": {
                "cached_at": "2025-12-01T10:00:00",
                "source": "ondeck",
                "subtitles": ["/cache/Movie.en.srt"]
            }
        }
        with open(ts_file, 'w') as f:
            json.dump(data, f, indent=2)

        tracker = CacheTimestampTracker(ts_file)

        # Should have migrated
        entry = tracker.get_entry("/cache/Movie.mkv")
        assert "associated_files" in entry
        assert "subtitles" not in entry
        assert "/cache/Movie.en.srt" in entry["associated_files"]

    def test_reverse_index_built_after_migration(self, temp_dir):
        """Reverse index works after subtitles→associated_files migration."""
        ts_file = os.path.join(temp_dir, "timestamps.json")
        data = {
            "/cache/Movie.mkv": {
                "cached_at": "2025-12-01T10:00:00",
                "source": "ondeck",
                "subtitles": ["/cache/Movie.en.srt"]
            }
        }
        with open(ts_file, 'w') as f:
            json.dump(data, f, indent=2)

        tracker = CacheTimestampTracker(ts_file)
        assert tracker.find_parent_video("/cache/Movie.en.srt") == "/cache/Movie.mkv"


# ============================================================
# CacheTimestampTracker associate_files tests
# ============================================================

class TestAssociateFiles:
    def test_associate_mixed_file_types(self, temp_dir):
        """associate_files handles subtitles, artwork, and NFOs."""
        ts_file = os.path.join(temp_dir, "timestamps.json")
        data = {
            "/cache/Movie.mkv": {
                "cached_at": "2025-12-01T10:00:00",
                "source": "ondeck"
            }
        }
        with open(ts_file, 'w') as f:
            json.dump(data, f, indent=2)

        tracker = CacheTimestampTracker(ts_file)
        tracker.associate_files({
            "/cache/Movie.mkv": [
                "/cache/Movie.en.srt",
                "/cache/poster.jpg",
                "/cache/Movie.nfo"
            ]
        })

        files = tracker.get_associated_files("/cache/Movie.mkv")
        assert "/cache/Movie.en.srt" in files
        assert "/cache/poster.jpg" in files
        assert "/cache/Movie.nfo" in files

    def test_backward_compat_associate_subtitles(self, temp_dir):
        """associate_subtitles still works (alias)."""
        ts_file = os.path.join(temp_dir, "timestamps.json")
        data = {
            "/cache/Movie.mkv": {
                "cached_at": "2025-12-01T10:00:00",
                "source": "ondeck"
            }
        }
        with open(ts_file, 'w') as f:
            json.dump(data, f, indent=2)

        tracker = CacheTimestampTracker(ts_file)
        tracker.associate_subtitles({
            "/cache/Movie.mkv": ["/cache/Movie.en.srt"]
        })
        assert "/cache/Movie.en.srt" in tracker.get_associated_files("/cache/Movie.mkv")

    def test_backward_compat_get_subtitles(self, temp_dir):
        """get_subtitles returns associated_files (alias)."""
        ts_file = os.path.join(temp_dir, "timestamps.json")
        data = {
            "/cache/Movie.mkv": {
                "cached_at": "2025-12-01T10:00:00",
                "source": "ondeck",
                "associated_files": ["/cache/Movie.en.srt", "/cache/poster.jpg"]
            }
        }
        with open(ts_file, 'w') as f:
            json.dump(data, f, indent=2)

        tracker = CacheTimestampTracker(ts_file)
        subs = tracker.get_subtitles("/cache/Movie.mkv")
        assert "/cache/Movie.en.srt" in subs
        assert "/cache/poster.jpg" in subs


# ============================================================
# Reference counting tests
# ============================================================

class TestReferenceCount:
    def test_get_other_videos_in_directory(self, temp_dir):
        """get_other_videos_in_directory finds sibling videos."""
        ts_file = os.path.join(temp_dir, "timestamps.json")
        data = {
            "/cache/Show/Season 1/S01E01.mkv": {
                "cached_at": "2025-12-01T10:00:00",
                "source": "ondeck"
            },
            "/cache/Show/Season 1/S01E02.mkv": {
                "cached_at": "2025-12-01T10:00:00",
                "source": "ondeck"
            }
        }
        with open(ts_file, 'w') as f:
            json.dump(data, f, indent=2)

        tracker = CacheTimestampTracker(ts_file)
        others = tracker.get_other_videos_in_directory(
            "/cache/Show/Season 1",
            excluding="/cache/Show/Season 1/S01E01.mkv"
        )
        assert "/cache/Show/Season 1/S01E02.mkv" in others
        assert "/cache/Show/Season 1/S01E01.mkv" not in others

    def test_get_other_videos_empty_when_last(self, temp_dir):
        """No other videos when only one exists."""
        ts_file = os.path.join(temp_dir, "timestamps.json")
        data = {
            "/cache/Movie/Movie.mkv": {
                "cached_at": "2025-12-01T10:00:00",
                "source": "ondeck"
            }
        }
        with open(ts_file, 'w') as f:
            json.dump(data, f, indent=2)

        tracker = CacheTimestampTracker(ts_file)
        others = tracker.get_other_videos_in_directory(
            "/cache/Movie",
            excluding="/cache/Movie/Movie.mkv"
        )
        assert others == []

    def test_reassociate_file(self, temp_dir):
        """reassociate_file moves a file from one parent to another."""
        ts_file = os.path.join(temp_dir, "timestamps.json")
        data = {
            "/cache/Show/Season 1/S01E01.mkv": {
                "cached_at": "2025-12-01T10:00:00",
                "source": "ondeck",
                "associated_files": ["/cache/Show/Season 1/poster.jpg"]
            },
            "/cache/Show/Season 1/S01E02.mkv": {
                "cached_at": "2025-12-01T10:00:00",
                "source": "ondeck"
            }
        }
        with open(ts_file, 'w') as f:
            json.dump(data, f, indent=2)

        tracker = CacheTimestampTracker(ts_file)
        tracker.reassociate_file(
            "/cache/Show/Season 1/poster.jpg",
            from_parent="/cache/Show/Season 1/S01E01.mkv",
            to_parent="/cache/Show/Season 1/S01E02.mkv"
        )

        # poster.jpg should be moved
        assert "/cache/Show/Season 1/poster.jpg" not in tracker.get_associated_files("/cache/Show/Season 1/S01E01.mkv")
        assert "/cache/Show/Season 1/poster.jpg" in tracker.get_associated_files("/cache/Show/Season 1/S01E02.mkv")
        # Reverse index should be updated
        assert tracker.find_parent_video("/cache/Show/Season 1/poster.jpg") == "/cache/Show/Season 1/S01E02.mkv"


# ============================================================
# .plexcached three-way category matching
# ============================================================

class TestPlexcachedCategoryMatching:
    def test_video_matches_video(self, temp_dir):
        """Video .plexcached matches video source."""
        # Create array directory with .plexcached file
        array_dir = os.path.join(temp_dir, "array", "Movies", "Movie (2020)")
        os.makedirs(array_dir, exist_ok=True)
        create_test_file(
            os.path.join(array_dir, "Movie (2020) [WEBDL-1080p].mkv" + PLEXCACHED_EXTENSION),
            "backup"
        )
        result = find_matching_plexcached(
            array_dir,
            "Movie (2020)",
            "Movie (2020) [HEVC-1080p].mkv"
        )
        assert result is not None

    def test_sidecar_does_not_match_video(self, temp_dir):
        """A sidecar .plexcached should NOT match a video source."""
        array_dir = os.path.join(temp_dir, "array", "Movies", "Movie (2020)")
        os.makedirs(array_dir, exist_ok=True)
        create_test_file(
            os.path.join(array_dir, "Movie (2020).nfo" + PLEXCACHED_EXTENSION),
            "backup"
        )
        result = find_matching_plexcached(
            array_dir,
            "Movie (2020)",
            "Movie (2020) [HEVC-1080p].mkv"
        )
        assert result is None

    def test_sidecar_matches_sidecar(self, temp_dir):
        """A sidecar .plexcached matches a sidecar source."""
        array_dir = os.path.join(temp_dir, "array", "Movies", "Movie (2020)")
        os.makedirs(array_dir, exist_ok=True)
        create_test_file(
            os.path.join(array_dir, "Movie (2020).nfo" + PLEXCACHED_EXTENSION),
            "backup"
        )
        result = find_matching_plexcached(
            array_dir,
            "Movie (2020)",
            "Movie (2020).nfo"
        )
        assert result is not None


# ============================================================
# Priority delegation for non-video files
# ============================================================

class TestPriorityDelegation:
    def test_artwork_delegates_to_parent(self, temp_dir):
        """Non-video files (artwork) delegate priority to parent video."""
        ts_file = os.path.join(temp_dir, "timestamps.json")
        data = {
            "/cache/Movie.mkv": {
                "cached_at": datetime.now().isoformat(),
                "source": "ondeck",
                "associated_files": ["/cache/poster.jpg"]
            }
        }
        with open(ts_file, 'w') as f:
            json.dump(data, f, indent=2)

        tracker = CacheTimestampTracker(ts_file)
        ondeck = OnDeckTracker(os.path.join(temp_dir, "ondeck.json"))
        watchlist = WatchlistTracker(os.path.join(temp_dir, "watchlist.json"))

        priority_mgr = CachePriorityManager(tracker, watchlist, ondeck)

        video_priority = priority_mgr.calculate_priority("/cache/Movie.mkv")
        artwork_priority = priority_mgr.calculate_priority("/cache/poster.jpg")
        assert artwork_priority == video_priority

    def test_nfo_delegates_to_parent(self, temp_dir):
        """NFO files delegate priority to parent video."""
        ts_file = os.path.join(temp_dir, "timestamps.json")
        data = {
            "/cache/Movie.mkv": {
                "cached_at": datetime.now().isoformat(),
                "source": "ondeck",
                "associated_files": ["/cache/Movie.nfo"]
            }
        }
        with open(ts_file, 'w') as f:
            json.dump(data, f, indent=2)

        tracker = CacheTimestampTracker(ts_file)
        ondeck = OnDeckTracker(os.path.join(temp_dir, "ondeck.json"))
        watchlist = WatchlistTracker(os.path.join(temp_dir, "watchlist.json"))

        priority_mgr = CachePriorityManager(tracker, watchlist, ondeck)

        video_priority = priority_mgr.calculate_priority("/cache/Movie.mkv")
        nfo_priority = priority_mgr.calculate_priority("/cache/Movie.nfo")
        assert nfo_priority == video_priority


# ============================================================
# Tracker cleanup with associated_files
# ============================================================

class TestTrackerCleanup:
    def test_remove_parent_clears_associated_files(self, temp_dir):
        """Removing a parent video clears associated files from reverse index."""
        ts_file = os.path.join(temp_dir, "timestamps.json")
        data = {
            "/cache/Movie.mkv": {
                "cached_at": "2025-12-01T10:00:00",
                "source": "ondeck",
                "associated_files": ["/cache/Movie.en.srt", "/cache/poster.jpg"]
            }
        }
        with open(ts_file, 'w') as f:
            json.dump(data, f, indent=2)

        tracker = CacheTimestampTracker(ts_file)
        assert tracker.find_parent_video("/cache/Movie.en.srt") == "/cache/Movie.mkv"

        tracker.remove_entry("/cache/Movie.mkv")
        assert tracker.find_parent_video("/cache/Movie.en.srt") is None

    def test_remove_associated_file(self, temp_dir):
        """Removing an associated file removes it from parent's list."""
        ts_file = os.path.join(temp_dir, "timestamps.json")
        data = {
            "/cache/Movie.mkv": {
                "cached_at": "2025-12-01T10:00:00",
                "source": "ondeck",
                "associated_files": ["/cache/Movie.en.srt", "/cache/poster.jpg"]
            }
        }
        with open(ts_file, 'w') as f:
            json.dump(data, f, indent=2)

        tracker = CacheTimestampTracker(ts_file)
        tracker.remove_entry("/cache/poster.jpg")

        files = tracker.get_associated_files("/cache/Movie.mkv")
        assert "/cache/poster.jpg" not in files
        assert "/cache/Movie.en.srt" in files


# ============================================================
# Retention delegation for non-video files
# ============================================================

class TestRetentionDelegation:
    def test_artwork_inherits_parent_retention(self, temp_dir):
        """Non-video associated files inherit parent's retention period."""
        ts_file = os.path.join(temp_dir, "timestamps.json")
        data = {
            "/cache/Movie.mkv": {
                "cached_at": datetime.now().isoformat(),
                "source": "ondeck",
                "associated_files": ["/cache/poster.jpg"]
            }
        }
        with open(ts_file, 'w') as f:
            json.dump(data, f, indent=2)

        tracker = CacheTimestampTracker(ts_file)
        # Parent is within retention
        assert tracker.is_within_retention_period("/cache/Movie.mkv", 24)
        # Artwork should also be within retention via delegation
        assert tracker.is_within_retention_period("/cache/poster.jpg", 24)
