"""The media server interface (core/media_server.py) and how a run uses it.

A run asks every enabled server through one interface instead of calling the
Plex and Jellyfin clients by name, so a Jellyfin-only install can drop Plex
later (docs/media-servers-design.md). This step changes no behaviour: the
same calls are made with the same arguments, Plex first, and move-back waits
for the same reasons with the same messages.
"""

import logging
import os
import sys
from unittest.mock import MagicMock

import pytest

for _mod_name in ['plexapi', 'plexapi.server', 'plexapi.video', 'plexapi.myplex']:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.media_server import (  # noqa: E402
    MEDIA_SERVER_KEYS, ActiveSessions, JellyfinServer, OnDeckItem, PlexServer, WatchlistFetch,
    complete_watchlist_sources,
)


def _config(**overrides):
    config = MagicMock()
    config.plex.valid_sections = [1, 2]
    config.plex.users_toggle = True
    config.plex.skip_ondeck = ["kid"]
    config.plex.skip_watchlist = ["guest"]
    config.plex.per_user_ondeck_days = {"anna": 3}
    config.plex.users = [{"title": "anna", "is_local": True}, {"title": "remote", "is_local": False}]
    config.cache.remote_watchlist_toggle = False
    config.cache.remote_watchlist_rss_url = ""
    config.jellyfin.favorites_toggle = False
    for path, value in overrides.items():
        target = config
        *parents, last = path.split(".")
        for part in parents:
            target = getattr(target, part)
        setattr(target, last, value)
    return config


# -- shared types ---------------------------------------------------------------

def test_old_import_paths_still_work():
    from core.jellyfin_api import ActiveSessions as JellyfinSessions, WatchlistItem
    from core.plex_api import OnDeckItem as PlexOnDeckItem
    assert PlexOnDeckItem is OnDeckItem and JellyfinSessions is ActiveSessions
    assert WatchlistItem is not None


def test_every_known_server_counts_as_complete_unless_reported_otherwise():
    assert MEDIA_SERVER_KEYS == ("plex", "jellyfin")
    assert complete_watchlist_sources([]) == {"plex", "jellyfin"}
    assert complete_watchlist_sources(["jellyfin"]) == {"plex"}


# -- Plex -----------------------------------------------------------------------

class TestPlexServer:
    def test_on_deck_passes_the_plex_settings(self):
        manager = MagicMock()
        manager.get_on_deck_media.return_value = [OnDeckItem("/data/a.mkv", "anna")]
        items = PlexServer(manager, _config(), lambda s: None).get_on_deck(30, 5, 10)
        manager.get_on_deck_media.assert_called_once_with(
            [1, 2], 30, 5, True, ["kid"], per_user_days={"anna": 3}, prefetch_minimum_minutes=10)
        assert [i.file_path for i in items] == ["/data/a.mkv"]

    def test_on_deck_with_unset_lists(self):
        manager = MagicMock()
        manager.get_on_deck_media.return_value = []
        PlexServer(manager, _config(**{"plex.valid_sections": None, "plex.skip_ondeck": None}),
                   lambda s: None).get_on_deck(30, 5, 10)
        args = manager.get_on_deck_media.call_args.args
        assert args[0] == [] and args[4] == []

    def test_watchlist_home_users_then_rss(self):
        manager = MagicMock()
        local = [("/data/l.mkv", "anna", None, None, "1", "movie")]
        remote = [("/data/r.mkv", "rss", None, None, None, "movie")]
        manager.get_watchlist_media.side_effect = lambda *a, rss_url=None, **k: remote if rss_url else local
        manager.is_watchlist_data_complete.return_value = True
        config = _config(**{"cache.remote_watchlist_toggle": True, "cache.remote_watchlist_rss_url": "https://rss"})

        fetch = PlexServer(manager, config, lambda s: None).get_watchlist(4)

        assert fetch == WatchlistFetch(local + remote, True, 1)
        first = manager.get_watchlist_media.call_args_list[0]
        assert first.args == ([1, 2], 4, True, ["guest"]) and first.kwargs == {"home_users": ["anna"]}
        assert manager.get_watchlist_media.call_args_list[1].kwargs["users_toggle"] is False

    def test_rss_failure_keeps_the_home_users_and_marks_the_fetch_incomplete(self, caplog):
        manager = MagicMock()
        manager.get_watchlist_media.side_effect = (
            lambda *a, rss_url=None, **k: (_ for _ in ()).throw(OSError("down")) if rss_url
            else [("/data/l.mkv", "anna", None, None, "1", "movie")])
        manager.is_watchlist_data_complete.return_value = True
        config = _config(**{"cache.remote_watchlist_toggle": True, "cache.remote_watchlist_rss_url": "https://rss"})
        server = PlexServer(manager, config, lambda s: None)

        with caplog.at_level(logging.ERROR):
            fetch = server.get_watchlist(4)

        assert [i[0] for i in fetch.items] == ["/data/l.mkv"] and fetch.complete is False
        assert "Failed to fetch remote watchlist via RSS" in caplog.text
        # Move-back only waits for plex.tv, not for the RSS feed
        assert server.is_watchlist_complete() is True

    def test_sessions_resolve_to_paths_and_all_count(self, caplog):
        manager = MagicMock()
        manager.get_active_sessions.return_value = ["ok", "no-path", "broken"]

        def session_path(session):
            if session == "broken":
                raise AttributeError("no media")
            return "/data/ok.mkv" if session == "ok" else None

        with caplog.at_level(logging.ERROR):
            sessions = PlexServer(manager, _config(), session_path).get_active_sessions()

        assert sessions == ActiveSessions(3, ["/data/ok.mkv"])
        assert "Error processing session broken" in caplog.text


# -- Jellyfin -------------------------------------------------------------------

class TestJellyfinServer:
    def test_favorites_off_asks_nothing_and_is_complete(self):
        manager = MagicMock()
        server = JellyfinServer(manager, _config())
        assert server.get_watchlist(3) == WatchlistFetch()
        manager.get_favorite_media.assert_not_called()
        assert server.is_watchlist_complete()

    def test_favorites_on(self):
        manager = MagicMock()
        manager.get_favorite_media.return_value = [("/data/f.mkv", "roger", None, None, None, "movie")]
        manager.is_favorites_data_complete.return_value = False
        server = JellyfinServer(manager, _config(**{"jellyfin.favorites_toggle": True}))
        fetch = server.get_watchlist(3)
        manager.get_favorite_media.assert_called_once_with(3)
        assert fetch.items == [("/data/f.mkv", "roger", None, None, None, "movie")] and fetch.complete is False
        assert server.is_watchlist_complete() is False

    def test_on_deck_passes_through(self):
        manager = MagicMock()
        manager.get_on_deck_media.return_value = []
        JellyfinServer(manager, _config()).get_on_deck(30, 5, 10)
        manager.get_on_deck_media.assert_called_once_with(30, 5, prefetch_minimum_minutes=10)


# -- the run --------------------------------------------------------------------

def _app(plex=True, jellyfin=True, watchlist_toggle=True, favorites=True):
    from core.app import PlexCacheApp
    app = object.__new__(PlexCacheApp)
    app.config_manager = _config(**{"jellyfin.favorites_toggle": favorites})
    app.config_manager.cache.watchlist_toggle = watchlist_toggle
    app.plex_manager = MagicMock() if plex else None
    app.jellyfin_manager = MagicMock() if jellyfin else None
    for manager in (app.plex_manager, app.jellyfin_manager):
        if manager is not None:
            manager.is_ondeck_data_complete.return_value = True
            manager.is_watchlist_data_complete.return_value = True
            manager.is_favorites_data_complete.return_value = True
    return app


class TestRunUsesTheServers:
    def test_plex_first_then_jellyfin_when_set_up(self):
        assert [s.key for s in _app().media_servers] == ["plex", "jellyfin"]
        assert [s.key for s in _app(jellyfin=False).media_servers] == ["plex"]
        assert [s.key for s in _app(plex=False).media_servers] == ["jellyfin"]
        assert _app(plex=False, jellyfin=False).media_servers == []

    def test_move_back_goes_ahead_when_everything_answered(self):
        assert _app()._move_back_wait_reason() is None

    @pytest.mark.parametrize("broken, expected", [
        ("plex.is_ondeck_data_complete", "OnDeck data incomplete (Plex server unreachable)"),
        ("jellyfin.is_ondeck_data_complete", "Jellyfin OnDeck data incomplete"),
        ("plex.is_watchlist_data_complete", "watchlist data incomplete (plex.tv unreachable)"),
        ("jellyfin.is_favorites_data_complete", "Jellyfin favorites incomplete"),
    ])
    def test_move_back_waits_with_the_same_message(self, broken, expected):
        app = _app()
        server, method = broken.split(".")
        getattr(getattr(app, f"{server}_manager"), method).return_value = False
        assert expected in _app_reason(app)

    def test_onDeck_is_checked_before_any_watchlist(self):
        app = _app()
        app.plex_manager.is_watchlist_data_complete.return_value = False
        app.jellyfin_manager.is_ondeck_data_complete.return_value = False
        assert "Jellyfin OnDeck data incomplete" in _app_reason(app)

    def test_watchlist_only_matters_when_watchlist_caching_is_on(self):
        app = _app(watchlist_toggle=False)
        app.plex_manager.is_watchlist_data_complete.return_value = False
        assert app._move_back_wait_reason() is None

    def test_favorites_off_never_hold_move_back(self):
        app = _app(favorites=False)
        app.jellyfin_manager.is_favorites_data_complete.return_value = False
        assert app._move_back_wait_reason() is None


def _app_reason(app):
    reason = app._move_back_wait_reason()
    assert reason is not None and len(reason) == 2
    return reason[0]
