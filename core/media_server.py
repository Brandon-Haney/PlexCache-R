"""Media servers PlexCache-D asks what to cache: Plex, Jellyfin, or both.

A run loops over the enabled servers through one interface, ``MediaServer``,
instead of calling each server's client by name. ``PlexServer`` and
``JellyfinServer`` adapt the existing clients (``core.plex_api.PlexManager``,
``core.jellyfin_api.JellyfinManager``) to it; the clients themselves are
unchanged.

Paths a server reports are in that server's path space. For now Jellyfin
translates its paths into Plex's (``jellyfin_path_mappings``), so one path
conversion serves both; per-server mappings come later.

Design: docs/media-servers-design.md.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Protocol, Sequence, Tuple

# Every server PlexCache-D knows, in the order a run asks them.
MEDIA_SERVER_KEYS: Tuple[str, ...] = ("plex", "jellyfin")


@dataclass
class OnDeckItem:
    """Represents an OnDeck item with metadata.

    Attributes:
        file_path: Path to the media file.
        username: The user who has this on their OnDeck.
        episode_info: For TV episodes, dict with 'show', 'season', 'episode' keys.
        is_current_ondeck: True if this is the actual OnDeck episode (not prefetched next).
        source: Media server that reported it ("plex" or "jellyfin").
    """
    file_path: str
    username: str
    episode_info: Optional[Dict[str, any]] = None
    is_current_ondeck: bool = False
    rating_key: Optional[str] = None
    source: str = "plex"


# (file_path, username, watchlisted_at, episode_info, rating_key, media_type)
WatchlistItem = Tuple[str, str, Optional[datetime], Optional[Dict[str, Any]], Optional[str], str]


class ActiveSessions(NamedTuple):
    """Playback sessions on one server. count includes sessions whose path is unknown or unmapped."""
    count: int
    paths: List[str]


@dataclass
class WatchlistFetch:
    """What one server reported for the watchlist this run.

    complete: every request answered, so an item that is missing really
        dropped off and its source label can go.
    remote_count: items that came from the Plex RSS feed (remote users).
    """
    items: List[WatchlistItem] = field(default_factory=list)
    complete: bool = True
    remote_count: int = 0


class MediaServer(Protocol):
    """One media server, as a run sees it."""

    key: str      # "plex" | "jellyfin", stored in trackers as the source
    label: str    # "Plex" | "Jellyfin", for logs and messages
    # Logged (as two WARNING lines) when move-back is skipped because of this server
    ondeck_incomplete_warning: Tuple[str, str]
    watchlist_incomplete_warning: Tuple[str, str]

    def get_on_deck(self, days_to_monitor: int, number_episodes: int,
                    prefetch_minimum_minutes: int) -> List[OnDeckItem]: ...

    def get_watchlist(self, watchlist_episodes: int) -> WatchlistFetch: ...

    def get_active_sessions(self) -> ActiveSessions: ...

    def is_ondeck_complete(self) -> bool: ...

    def is_watchlist_complete(self) -> bool: ...


def complete_watchlist_sources(incomplete: Sequence[str]) -> set:
    """Servers whose watchlist this run is complete, so a missing item really dropped off.

    A server that isn't set up counts as complete: it reports nothing, and old
    labels from it should go. A server with an incomplete fetch keeps its
    recorded sources, so an outage never relabels the badges.
    """
    return set(MEDIA_SERVER_KEYS) - set(incomplete)


class PlexServer:
    """Plex through ``PlexManager``: OnDeck, watchlist (home users and RSS), sessions."""

    key = "plex"
    label = "Plex"
    ondeck_incomplete_warning = (
        "Skipping array restore - OnDeck data incomplete (Plex server unreachable)",
        "Files will remain on cache until next successful run",
    )
    watchlist_incomplete_warning = (
        "Skipping array restore - watchlist data incomplete (plex.tv unreachable)",
        "Files will remain on cache until next successful run",
    )

    def __init__(self, manager: Any, config_manager: Any,
                 session_path: Callable[[Any], Optional[str]]):
        """session_path: turns a plexapi session into the playing file's Plex path."""
        self.manager = manager
        self.config = config_manager
        self._session_path = session_path

    def get_on_deck(self, days_to_monitor: int, number_episodes: int,
                    prefetch_minimum_minutes: int) -> List[OnDeckItem]:
        plex = self.config.plex
        return self.manager.get_on_deck_media(
            plex.valid_sections or [],
            days_to_monitor,
            number_episodes,
            plex.users_toggle,
            plex.skip_ondeck or [],
            per_user_days=plex.per_user_ondeck_days,
            prefetch_minimum_minutes=prefetch_minimum_minutes,
        )

    def get_watchlist(self, watchlist_episodes: int) -> WatchlistFetch:
        """Home users' watchlists, then remote users through the RSS feed."""
        plex, cache = self.config.plex, self.config.cache
        # Only home users have watchlists the API can read
        home_users = [u.get("title") for u in plex.users if u.get("is_local", False)]
        items = list(self.manager.get_watchlist_media(
            plex.valid_sections,
            watchlist_episodes,
            plex.users_toggle,
            plex.skip_watchlist,
            home_users=home_users,
        ))

        rss_failed = False
        remote_count = 0
        if cache.remote_watchlist_toggle and cache.remote_watchlist_rss_url:
            logging.debug("Fetching watchlist via RSS feed for remote users...")
            try:
                remote_items = list(self.manager.get_watchlist_media(
                    valid_sections=plex.valid_sections,
                    watchlist_episodes=watchlist_episodes,
                    users_toggle=False,  # only RSS, no local Plex users
                    skip_watchlist=plex.skip_watchlist,
                    rss_url=cache.remote_watchlist_rss_url,
                ))
                logging.debug(f"Found {len(remote_items)} remote watchlist items from RSS")
                items.extend(remote_items)
                remote_count = len(remote_items)
            except Exception as e:
                rss_failed = True
                logging.error(f"Failed to fetch remote watchlist via RSS: {str(e)}")

        complete = bool(self.manager.is_watchlist_data_complete()) and not rss_failed
        return WatchlistFetch(items, complete, remote_count)

    def get_active_sessions(self) -> ActiveSessions:
        sessions = self.manager.get_active_sessions()
        paths = []
        for session in sessions:
            try:
                path = self._session_path(session)
                if path:
                    paths.append(path)
            except (AttributeError, KeyError, TypeError, OSError) as e:
                logging.error(f"Error processing session {session}: {type(e).__name__}: {e}")
        return ActiveSessions(len(sessions), paths)

    def is_ondeck_complete(self) -> bool:
        return self.manager.is_ondeck_data_complete()

    def is_watchlist_complete(self) -> bool:
        """plex.tv answered. An RSS failure only keeps source labels (see get_watchlist)."""
        return self.manager.is_watchlist_data_complete()


class JellyfinServer:
    """Jellyfin through ``JellyfinManager``: Continue Watching and Next Up, favorites, sessions."""

    key = "jellyfin"
    label = "Jellyfin"
    ondeck_incomplete_warning = (
        "Skipping array restore - Jellyfin OnDeck data incomplete (Jellyfin unreachable "
        "or a request failed, see errors above)",
        "Plex files are held on cache too until Jellyfin answers; "
        "remove the Jellyfin URL/API key to stop using it as an OnDeck source",
    )
    watchlist_incomplete_warning = (
        "Skipping array restore - Jellyfin favorites incomplete (Jellyfin unreachable "
        "or a request failed, see errors above)",
        "Files will remain on cache until next successful run",
    )

    def __init__(self, manager: Any, config_manager: Any):
        self.manager = manager
        self.config = config_manager

    @property
    def _favorites_on(self) -> bool:
        return bool(self.config.jellyfin.favorites_toggle)

    def get_on_deck(self, days_to_monitor: int, number_episodes: int,
                    prefetch_minimum_minutes: int) -> List[OnDeckItem]:
        items = self.manager.get_on_deck_media(
            days_to_monitor, number_episodes, prefetch_minimum_minutes=prefetch_minimum_minutes)
        logging.info(f"[FETCH] Jellyfin OnDeck: {len(items)} items")
        return items

    def get_watchlist(self, watchlist_episodes: int) -> WatchlistFetch:
        """Unplayed favorites stand in for a watchlist, which Jellyfin doesn't have."""
        if not self._favorites_on:
            return WatchlistFetch()
        favorites = self.manager.get_favorite_media(watchlist_episodes)
        logging.info(f"[FETCH] Jellyfin favorites: {len(favorites)} items")
        return WatchlistFetch(list(favorites), bool(self.manager.is_favorites_data_complete()))

    def get_active_sessions(self) -> ActiveSessions:
        return self.manager.get_active_sessions()

    def is_ondeck_complete(self) -> bool:
        return self.manager.is_ondeck_data_complete()

    def is_watchlist_complete(self) -> bool:
        return not self._favorites_on or self.manager.is_favorites_data_complete()
