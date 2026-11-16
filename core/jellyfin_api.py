"""
Jellyfin API integration for PlexCache.
Optional second OnDeck source: Continue Watching + Next Up of every Jellyfin
user, plus active sessions. Jellyfin paths are translated into Plex paths so
the regular path_mappings handle them like any Plex item.
"""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import requests

from core.media_server import (  # noqa: F401 (re-exported)
    ActiveSessions, OnDeckItem, RecentlyAddedItem, RecentlyAddedResult, WatchlistItem, next_episodes_after,
)

REQUEST_TIMEOUT = 30
ITEM_FIELDS = "Path,MediaSources"
# RunTimeTicks are 100ns units
TICKS_PER_MS = 10_000


# Jellyfin CollectionType -> the type names Plex libraries use, so the Web UI treats them alike
_LIBRARY_TYPES = {"movies": "movie", "tvshows": "show", "music": "artist", "homevideos": "photo",
                  "photos": "photo"}
_LIBRARY_TYPE_LABELS = {"movies": "Movies", "tvshows": "TV Shows", "music": "Music",
                        "homevideos": "Home Videos", "photos": "Photos", "mixed": "Mixed", "boxsets": "Collections",
                        "musicvideos": "Music Videos", "books": "Books"}


def _normalize_prefix(prefix: str) -> str:
    """Drop trailing slashes so '/media/' and '/media' behave the same; '/' becomes ''."""
    return prefix.rstrip('/')


def _library_hint(path: str) -> str:
    """First two folders of a path (e.g. /media/TV/), enough to tell which mapping is missing."""
    parts = path.lstrip('/').split('/')[:-1][:2]
    return '/' + ''.join(f"{part}/" for part in parts)


def _parse_jellyfin_date(value: Optional[str]) -> Optional[datetime]:
    """Parse a Jellyfin UTC timestamp (7 fractional digits + 'Z') to second precision."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:19]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def jellyfin_path_table(jellyfin_config: Any, path_mappings: List[Any]) -> Tuple[Dict[str, str], List[str]]:
    """Where Jellyfin's paths go, and the Jellyfin folders of disabled libraries.

    A path mapping's jellyfin_path maps to that mapping's plex_path, so a file
    both servers report keeps one tracker entry, or to its real_path for a
    library only Jellyfin has (the run passes real paths through unchanged).
    The older jellyfin_path_mappings (Jellyfin prefix -> Plex prefix) still
    apply; a mapping's own jellyfin_path wins for the same prefix, including
    when its library is switched off.

    jellyfin_config is a core.config.JellyfinConfig, path_mappings a list of
    core.config.PathMapping. Used by the run and by the Web UI, so both read
    Jellyfin's paths the same way.
    """
    table = {prefix.rstrip('/'): target for prefix, target in (jellyfin_config.path_mappings or {}).items()}
    skipped = []
    for mapping in path_mappings:
        folder = (mapping.jellyfin_path or '').rstrip('/')
        if not folder:
            continue
        if mapping.enabled and mapping.jellyfin_enabled:
            table[folder] = mapping.plex_path or mapping.real_path
        else:
            table.pop(folder, None)
            skipped.append(folder)
    return table, skipped


def jellyfin_manager_from_config(config_manager: Any, **kwargs) -> "JellyfinManager":
    """A JellyfinManager with the URL, key, users and path table from a loaded ConfigManager."""
    jellyfin = config_manager.jellyfin
    table, skipped = jellyfin_path_table(jellyfin, config_manager.paths.path_mappings)
    return JellyfinManager(url=jellyfin.url, api_key=jellyfin.api_key, path_mappings=table,
                           skip_users=jellyfin.skip_users, skip_prefixes=skipped, **kwargs)


def _source_resolution(source: Dict[str, Any]) -> str:
    """Resolution of a Jellyfin MediaSource's video, in the keys Plex uses ("4k", "1080", "720", "480", "sd")."""
    width = height = 0
    for stream in source.get('MediaStreams') or []:
        if stream.get('Type') == 'Video':
            width, height = max(width, stream.get('Width') or 0), max(height, stream.get('Height') or 0)
    # Width first: a 2.39:1 film in 4K is 3840x1608, in 1080p 1920x800
    if width >= 3200 or height >= 1600:
        return "4k"
    if width >= 1700 or height >= 900:
        return "1080"
    if width >= 1100 or height >= 650:
        return "720"
    if width >= 640 or height >= 400:
        return "480"
    return "sd"


def _source_as_media(source: Dict[str, Any]) -> SimpleNamespace:
    """A MediaSource shaped like a plexapi Media, so core.pinned_media.select_media_version can choose."""
    return SimpleNamespace(
        videoResolution=_source_resolution(source),
        # Plex reports kbps, Jellyfin bps; only the order matters
        bitrate=(source.get('Bitrate') or 0) // 1000,
        parts=[SimpleNamespace(file=source.get('Path'), size=int(source.get('Size') or 0))],
    )


def _version_label(media: SimpleNamespace) -> str:
    """Short label like Plex's version labels: "4k", "1080p", "SD"."""
    resolution = media.videoResolution
    return "SD" if resolution == "sd" else (resolution if resolution.endswith('k') else f"{resolution}p")


def _local_naive(value: Optional[datetime]) -> Optional[datetime]:
    """A UTC datetime as naive local time, the form Plex's addedAt has, so the two sort together."""
    return value.astimezone().replace(tzinfo=None) if value else None


class JellyfinManager:
    """Fetches OnDeck-equivalent media from a Jellyfin server via an admin API key."""

    def __init__(self, url: str, api_key: str, path_mappings: Optional[Dict[str, str]] = None,
                 skip_users: Optional[List[str]] = None, session: Optional[Any] = None,
                 skip_prefixes: Optional[List[str]] = None):
        """path_mappings: Jellyfin path prefix -> the path the run uses for those files
        (a Plex path, or a real path for a library only Jellyfin has).
        skip_prefixes: Jellyfin folders of disabled libraries, skipped without the
        "unmapped path" notice."""
        self.url = url.rstrip('/')
        # Longest prefix first, so nested mappings win
        self.path_mappings = sorted(
            ((_normalize_prefix(j), _normalize_prefix(p)) for j, p in (path_mappings or {}).items()),
            key=lambda kv: len(kv[0]), reverse=True)
        self.skip_prefixes = [_normalize_prefix(p) for p in (skip_prefixes or []) if _normalize_prefix(p)]
        self.skip_users = set(skip_users or [])
        self.session = session or requests.Session()
        self.session.headers['Authorization'] = f'MediaBrowser Client="PlexCache-D", Token="{api_key}"'
        self._ondeck_data_complete = True
        self._favorites_data_complete = True
        self._reported_unmapped = set()

    def is_ondeck_data_complete(self) -> bool:
        return self._ondeck_data_complete

    def is_favorites_data_complete(self) -> bool:
        return self._favorites_data_complete

    def _get(self, path: str, **params) -> Any:
        response = self.session.get(f"{self.url}{path}", params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()

    def to_plex_path(self, path: str) -> Optional[str]:
        """Translate a Jellyfin path into the path the run uses (a Plex path, or a real
        path for a Jellyfin-only library). None if no mapping matches."""
        for jellyfin_prefix, plex_prefix in self.path_mappings:
            # Match whole folders only, so /media does not also match /media2
            if path == jellyfin_prefix or path.startswith(jellyfin_prefix + '/'):
                return plex_prefix + path[len(jellyfin_prefix):]
        return None

    def _item_paths(self, item: Dict[str, Any]) -> List[str]:
        """Plex-space paths of all versions of an item."""
        paths = [s['Path'] for s in item.get('MediaSources') or [] if s.get('Path')]
        if not paths and item.get('Path'):
            paths = [item['Path']]
        result = []
        for path in dict.fromkeys(paths):
            plex_path = self.to_plex_path(path)
            if plex_path:
                result.append(plex_path)
            elif self._in_skipped_library(path):
                logging.debug(f"[JELLYFIN] {path} is in a disabled library, skipping")
            else:
                self._report_unmapped(path)
        return result

    def _library_is_mapped(self, locations: List[str]) -> bool:
        """Whether any of a library's folders, or a folder inside one, has a path mapping."""
        for folder in (_normalize_prefix(location) for location in locations):
            if self.to_plex_path(folder) or any(prefix.startswith(folder + '/') for prefix, _ in self.path_mappings):
                return True
        return False

    def _in_skipped_library(self, path: str) -> bool:
        return any(path == prefix or path.startswith(prefix + '/') for prefix in self.skip_prefixes)

    def _report_unmapped(self, path: str) -> None:
        """Log an unmapped path at INFO once per library folder, so a wrong mapping is visible."""
        logging.debug(f"[JELLYFIN] No jellyfin_path_mappings entry for {path}, skipping")
        hint = _library_hint(path)
        if hint not in self._reported_unmapped:
            self._reported_unmapped.add(hint)
            logging.info(f"[JELLYFIN] Skipping unmapped path {hint} - set it as the Jellyfin path "
                         f"of a path mapping (jellyfin_path), or add it to jellyfin_path_mappings")

    def get_on_deck_media(self, days_to_monitor: int, number_episodes: int,
                          prefetch_minimum_minutes: int = 0) -> List[OnDeckItem]:
        """Continue Watching + Next Up (with the next N episodes) for every enabled user.

        Any failure marks the data incomplete, so move-back is skipped instead of
        evicting media a Jellyfin user is still watching.
        """
        self._ondeck_data_complete = True
        users = self._users_to_fetch('OnDeck')
        if users is None:
            self._ondeck_data_complete = False
            return []

        on_deck_files: List[OnDeckItem] = []
        for user in users:
            username = user.get('Name', '')
            try:
                on_deck_files.extend(self._fetch_user_on_deck_media(
                    user['Id'], username, days_to_monitor, number_episodes, prefetch_minimum_minutes))
            except Exception as e:
                logging.error(f"[JELLYFIN] [USER:{username}] Failed to fetch OnDeck: {type(e).__name__}: {e}")
                self._ondeck_data_complete = False

        for item in on_deck_files:
            logging.debug(f"[JELLYFIN] [USER:{item.username}] OnDeck found: {item.file_path}")
        return on_deck_files

    def get_server_info(self) -> Dict[str, Any]:
        """Server name and version (GET /System/Info). Raises if Jellyfin can't be reached or the key is wrong."""
        return self._get('/System/Info')

    def list_libraries(self) -> List[Dict[str, Any]]:
        """Jellyfin libraries and their folders (GET /Library/VirtualFolders, needs an admin API key).

        Same shape as SettingsService.get_plex_libraries: id, title, type
        ('movie', 'show' or Jellyfin's own collection type), type_label, locations.
        """
        libraries = []
        for folder in self._get('/Library/VirtualFolders') or []:
            kind = folder.get('CollectionType') or 'mixed'
            libraries.append({
                "id": folder.get('ItemId'),
                "title": folder.get('Name', ''),
                "type": _LIBRARY_TYPES.get(kind, kind),
                "type_label": _LIBRARY_TYPE_LABELS.get(kind, kind.replace('tvshows', 'TV Shows').title()),
                "locations": [loc for loc in folder.get('Locations') or [] if loc],
                "server": "jellyfin",
            })
        return libraries

    def list_users(self) -> List[Dict[str, Any]]:
        """Every Jellyfin user (GET /Users): id, name, is_admin, is_disabled."""
        return [{
            "id": user.get('Id'),
            "name": user.get('Name', ''),
            "is_admin": bool((user.get('Policy') or {}).get('IsAdministrator')),
            "is_disabled": bool((user.get('Policy') or {}).get('IsDisabled')),
        } for user in self._get('/Users') or []]

    def get_recently_added_media(self, days_to_monitor: int, max_items: int,
                                 version_preference: str = "highest") -> RecentlyAddedResult:
        """Movies and episodes added in the last N days, newest first, like PlexManager's.

        Asks each movie or TV library whose folders are mapped (so a switched-off
        library is not asked) for its newest items, by DateCreated. One item per
        Jellyfin item, describing the version chosen by the same rule pins use
        (pinned_preferred_resolution); the others are listed in version_files.
        A library that fails is named in unreadable_libraries; if the library
        list itself can't be read, this raises.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_to_monitor)
        items: List[RecentlyAddedItem] = []
        unreadable: List[str] = []
        for library in self.list_libraries():
            if library['type'] not in ('movie', 'show', 'mixed'):
                continue
            if not self._library_is_mapped(library['locations']):
                continue
            try:
                found = self._get('/Items', parentId=library['id'], recursive='true',
                                  includeItemTypes='Movie,Episode', isMissing='false', sortBy='DateCreated',
                                  sortOrder='Descending', fields=f"{ITEM_FIELDS},DateCreated",
                                  limit=max_items).get('Items', [])
            except Exception as e:
                logging.warning(f"[JELLYFIN] Recently added: could not read library '{library['title']}': "
                                f"{type(e).__name__}: {e}")
                unreadable.append(library['title'])
                continue
            for item in found:
                added_at = _parse_jellyfin_date(item.get('DateCreated'))
                if added_at and added_at < cutoff:
                    break  # newest first, so the rest are older too
                try:
                    entry = self._recently_added_item(item, library, added_at, version_preference)
                except Exception as e:
                    logging.debug(f"[JELLYFIN] Recently added: skipping '{item.get('Name')}': {type(e).__name__}: {e}")
                    continue
                if entry:
                    items.append(entry)

        items.sort(key=lambda i: i.added_at or datetime.min, reverse=True)
        return RecentlyAddedResult(items=items[:max_items], unreadable_libraries=unreadable)

    def _recently_added_item(self, item: Dict[str, Any], library: Dict[str, Any], added_at: Optional[datetime],
                             version_preference: str) -> Optional[RecentlyAddedItem]:
        from core.pinned_media import select_media_version
        sources = [s for s in item.get('MediaSources') or [] if s.get('Path')]
        if not sources and item.get('Path'):
            sources = [{'Path': item['Path']}]
        if not sources:
            return None
        medias = [_source_as_media(source) for source in sources]
        preferred = select_media_version(SimpleNamespace(media=medias, title=item.get('Name', '')),
                                         version_preference)
        version_files = []
        for media in medias:
            path = self.to_plex_path(media.parts[0].file)
            if path:
                version_files.append({'file_path': path, 'size': media.parts[0].size,
                                      'is_preferred': media is preferred, 'label': _version_label(media)})
        preferred_files = [f for f in version_files if f['is_preferred']]
        if not preferred_files:
            return None

        episode_info = None
        media_type = 'episode' if item.get('Type') == 'Episode' else 'movie'
        if media_type == 'episode':
            episode_info = {'show': item.get('SeriesName'), 'season': item.get('ParentIndexNumber'),
                            'episode': item.get('IndexNumber')}
        return RecentlyAddedItem(
            file_path=preferred_files[0]['file_path'],
            rating_key=item.get('Id'),
            title=item.get('Name', ''),
            media_type=media_type,
            added_at=_local_naive(added_at),
            library_title=library['title'],
            library_section_id=library['id'],
            size=preferred_files[0]['size'],
            episode_info=episode_info,
            show_rating_key=item.get('SeriesId') or '',
            version_files=version_files,
            source='jellyfin',
        )

    def _users_to_fetch(self, purpose: str) -> Optional[List[Dict[str, Any]]]:
        """Enabled users minus jellyfin_skip_users. None if the user list can't be read."""
        try:
            users = self._get('/Users', isDisabled='false')
        except Exception as e:
            logging.error(f"[JELLYFIN] Failed to list users: {type(e).__name__}: {e}")
            return None
        result = []
        for user in users:
            if user.get('Name', '') in self.skip_users:
                logging.info(f"[JELLYFIN] [USER:{user.get('Name', '')}] Skipping for {purpose} — in skip list")
            else:
                result.append(user)
        return result

    def _fetch_user_on_deck_media(self, user_id: str, username: str, days_to_monitor: int,
                                  number_episodes: int, prefetch_minimum_minutes: int) -> List[OnDeckItem]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_to_monitor)
        resume = self._get_user_items(username, 'Continue Watching', '/UserItems/Resume', userId=user_id,
                                      fields=ITEM_FIELDS, mediaTypes='Video')
        # Next Up filters by the series' last played date server-side
        next_up = self._get_user_items(username, 'Next Up', '/Shows/NextUp', userId=user_id, fields=ITEM_FIELDS,
                                       nextUpDateCutoff=cutoff.strftime('%Y-%m-%dT%H:%M:%SZ'),
                                       enableResumable='true')
        if resume is None or next_up is None:
            self._ondeck_data_complete = False

        on_deck_files: List[OnDeckItem] = []
        seen_ids = set()
        for item in (resume or []) + (next_up or []):
            if item.get('Id') in seen_ids:
                continue
            seen_ids.add(item.get('Id'))
            last_played = _parse_jellyfin_date((item.get('UserData') or {}).get('LastPlayedDate'))
            if last_played and last_played < cutoff:
                continue
            # One failing item must not drop what was already collected for this user
            try:
                if item.get('Type') == 'Episode':
                    self._process_episode_ondeck(item, user_id, username, number_episodes,
                                                 prefetch_minimum_minutes, on_deck_files)
                elif item.get('Type') == 'Movie':
                    for path in self._item_paths(item):
                        on_deck_files.append(OnDeckItem(file_path=path, username=username,
                                                        is_current_ondeck=True, source='jellyfin'))
                else:
                    logging.debug(f"[JELLYFIN] Skipping OnDeck item '{item.get('Name')}' — type {item.get('Type')}")
            except Exception as e:
                logging.error(f"[JELLYFIN] [USER:{username}] Failed to process OnDeck item "
                              f"'{item.get('SeriesName') or item.get('Name')}': {type(e).__name__}: {e}")
                self._ondeck_data_complete = False
        return on_deck_files

    def _get_user_items(self, username: str, label: str, path: str, **params) -> Optional[List[Dict[str, Any]]]:
        """Fetch one item list; None on failure, so the caller can mark its data incomplete."""
        try:
            return self._get(path, **params).get('Items', [])
        except Exception as e:
            logging.error(f"[JELLYFIN] [USER:{username}] Failed to fetch {label}: {type(e).__name__}: {e}")
            return None

    def get_favorite_media(self, episodes_per_show: int) -> List[WatchlistItem]:
        """Unplayed favorite movies and the first unplayed episodes of favorite series, per user.

        Jellyfin has no watchlist, so favorites stand in for it. Played items are left
        out, since favorites are also used for things people have already watched.
        Jellyfin doesn't record when something was favorited, so watchlisted_at is None
        and the watchlist tracker counts retention from the first run that sees it.
        """
        self._favorites_data_complete = True
        users = self._users_to_fetch('favorites')
        if users is None:
            self._favorites_data_complete = False
            return []

        favorites: List[WatchlistItem] = []
        for user in users:
            username = user.get('Name', '')
            items = self._get_user_items(username, 'favorites', '/Items', userId=user['Id'], isFavorite='true',
                                         recursive='true', includeItemTypes='Movie,Series', isPlayed='false',
                                         fields=ITEM_FIELDS)
            if items is None:
                self._favorites_data_complete = False
                continue
            for item in items:
                # One failing item must not drop the user's other favorites
                try:
                    if item.get('Type') == 'Movie':
                        for path in self._item_paths(item):
                            favorites.append((path, username, None, None, None, 'movie'))
                    elif item.get('Type') == 'Series':
                        favorites.extend(self._favorite_series_episodes(item, user['Id'], username, episodes_per_show))
                except Exception as e:
                    logging.error(f"[JELLYFIN] [USER:{username}] Failed to process favorite "
                                  f"'{item.get('Name')}': {type(e).__name__}: {e}")
                    self._favorites_data_complete = False

        for path, username, *_ in favorites:
            logging.debug(f"[JELLYFIN] [USER:{username}] Favorite found: {path}")
        return favorites

    def _favorite_series_episodes(self, series: Dict[str, Any], user_id: str, username: str,
                                  episodes_per_show: int) -> List[WatchlistItem]:
        """First unplayed episodes of a series, season by season, without specials (season 0)."""
        seasons = self._get(f"/Shows/{series['Id']}/Seasons", userId=user_id).get('Items', [])
        # Jellyfin sorts specials first, so walk the regular seasons instead of sorting all episodes
        regular_seasons = sorted((s for s in seasons if (s.get('IndexNumber') or 0) > 0),
                                 key=lambda s: s['IndexNumber'])
        result: List[WatchlistItem] = []
        remaining = episodes_per_show
        for season in regular_seasons:
            if remaining <= 0:
                break
            episodes = self._get('/Items', userId=user_id, parentId=season['Id'], includeItemTypes='Episode',
                                 isPlayed='false', isMissing='false', sortBy='IndexNumber', limit=remaining,
                                 fields=ITEM_FIELDS).get('Items', [])
            for episode in episodes:
                episode_info = None
                if episode.get('ParentIndexNumber') is not None and episode.get('IndexNumber') is not None:
                    episode_info = {'show': series.get('Name'), 'season': episode['ParentIndexNumber'],
                                    'episode': episode['IndexNumber']}
                for path in self._item_paths(episode):
                    result.append((path, username, None, episode_info, None, 'episode'))
            remaining -= len(episodes)
        return result

    def _process_episode_ondeck(self, item: Dict[str, Any], user_id: str, username: str,
                                number_episodes: int, prefetch_minimum_minutes: int,
                                on_deck_files: List[OnDeckItem]) -> None:
        show = item.get('SeriesName')
        season = item.get('ParentIndexNumber')
        episode = item.get('IndexNumber')
        episode_info = None
        if season is not None and episode is not None:
            episode_info = {'show': show, 'season': season, 'episode': episode}

        for path in self._item_paths(item):
            on_deck_files.append(OnDeckItem(file_path=path, username=username,
                                            episode_info=episode_info, is_current_ondeck=True,
                                            source='jellyfin'))

        if episode_info is None or not item.get('SeriesId'):
            logging.warning(f"[JELLYFIN] Skipping next episode fetch for '{show}' - missing index data")
            return

        # startItemId skips everything before the current episode, keeping long shows small
        episodes_path = f"/Shows/{item['SeriesId']}/Episodes"
        episodes = self._get(episodes_path, userId=user_id, startItemId=item['Id'],
                             fields=ITEM_FIELDS, isMissing='false').get('Items', [])
        if not episodes:
            # Jellyfin <= 10.11 returns nothing when the item is an alternate version; fetch the full list instead
            episodes = self._get(episodes_path, userId=user_id, fields=ITEM_FIELDS,
                                 isMissing='false').get('Items', [])
        # Shape Jellyfin episodes like plexapi ones to share the prefetch logic
        candidates = [
            SimpleNamespace(parentIndex=e.get('ParentIndexNumber'), index=e.get('IndexNumber'),
                            title=e.get('Name'), grandparentTitle=show,
                            duration=(e.get('RunTimeTicks') or 0) // TICKS_PER_MS, item=e)
            for e in episodes
        ]
        next_episodes = next_episodes_after(
            candidates, season, episode, number_episodes,
            prefetch_minimum_minutes=prefetch_minimum_minutes)
        for next_episode in next_episodes:
            next_info = {'show': show, 'season': next_episode.parentIndex, 'episode': next_episode.index}
            for path in self._item_paths(next_episode.item):
                on_deck_files.append(OnDeckItem(file_path=path, username=username,
                                                episode_info=next_info, is_current_ondeck=False,
                                                source='jellyfin'))

    def get_active_sessions(self) -> ActiveSessions:
        """Everything currently playing on Jellyfin, with Plex-space paths where a mapping exists."""
        count = 0
        paths = []
        try:
            for session in self._get('/Sessions'):
                item = session.get('NowPlayingItem')
                if not item:
                    continue
                count += 1
                logging.debug(f"[JELLYFIN] Active session detected, skipping: {item.get('SeriesName') or item.get('Name')}")
                item_paths = self._item_paths(item)
                if not item_paths and not item.get('Path') and not item.get('MediaSources') and item.get('Id'):
                    # NowPlayingItem may come without path fields
                    items = self._get('/Items', ids=item['Id'], userId=session.get('UserId'),
                                      fields=ITEM_FIELDS).get('Items', [])
                    item_paths = [p for i in items for p in self._item_paths(i)]
                paths.extend(item_paths)
        except Exception as e:
            logging.warning(f"[JELLYFIN] Failed to fetch active sessions: {type(e).__name__}: {e}")
        return ActiveSessions(count, paths)
