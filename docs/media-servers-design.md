# Media servers: Plex, Jellyfin, or both

**Status:** phase 1 in progress (see the checklist under Phases).
**Branch:** `feature/media-servers` on Brandon-Haney/PlexCache-R, cut from upstream `main` at v3.3.0 (`e426fa9`).
**To review with:** StudioNirin (owner), RogerSik (wrote the Jellyfin source in #234).

## Goal

PlexCache-D works with **Plex, Jellyfin, or both, as equals**. A Jellyfin-only
install connects with a Jellyfin URL and API key and never needs a Plex server.
An install with both servers caches what either one's users are watching.

Plex installs must behave exactly as they do today. That was the condition
for #234 and it stays the rule for every change here.

**Not in scope:** Emby, changes to the caching, eviction or move-back rules,
and the product name (an open question for StudioNirin, see the end).

## Where things stand (v3.3.0)

#234 added Jellyfin as a second source next to Plex:

- Continue Watching (`/UserItems/Resume`) and Next Up (`/Shows/NextUp`) count as OnDeck, with the next episodes prefetched.
- Unplayed favorites can stand in for a watchlist (`jellyfin_favorites_toggle`).
- Files playing on Jellyfin are skipped, and the source badges show Plex or Jellyfin.
- It is configured through the settings file or `JELLYFIN_*` env vars. There is no Jellyfin screen in the Web UI.
- Jellyfin paths are translated into **Plex** paths (`jellyfin_path_mappings`), and the Plex path mappings then find the real files.

### What stops a Jellyfin-only install

| Where | What happens without Plex |
|---|---|
| `core/config.py:876-882, 964-972` | Settings validation requires `PLEX_URL`, `PLEX_TOKEN`, `valid_sections` and `users_toggle`, and rejects an empty URL or token. The run stops before it starts. |
| `web/routers/setup.py:53-62` | `is_setup_complete()` means "Plex URL and token are set", so every page redirects to setup. Setup step 2 must reach a live Plex. |
| `core/app.py:274, 1019-1041` | Every run starts with `_connect_to_plex()`. `PlexManager.connect()` raises `ConnectionError` and the run stops. Jellyfin is set up but never asked. |
| `core/app.py:1082-1103` | Active sessions call Plex with no guard. |
| `core/jellyfin_api.py:83-103` | Jellyfin paths only work through `jellyfin_path_mappings` into Plex path space. |
| `core/jellyfin_api.py:15` | The Jellyfin client imports `core.plex_api` (for `OnDeckItem` and the next-episode helper), which imports plexapi. |

### What is already server-neutral

The decisions don't call Plex. Only the layer that gathers the data does.

- **Move-back:** `core/file_operations.py:3838-3990` decides from the trackers and real paths. "Watched" means no longer on OnDeck or the watchlist, or behind the user's OnDeck position.
- **Priority and eviction:** `CachePriorityManager` (`core/file_operations.py:1927-2276`) uses the trackers only.
- **OnDeck tracker:** keyed by **real path** (`core/app.py:1232-1234`).
- **Shared fields:** `episode_info` {show, season, episode}, `source` / `sources` ("plex" / "jellyfin").

## Design

### 1. One interface for media servers

A new `core/media_server.py` holds what both servers share:

- **Types** moved out of `core/plex_api.py`:
  - `OnDeckItem`, unchanged fields.
  - `WatchlistItem`, a dataclass in place of today's 6-tuple.
  - `ActiveSessions(count, paths)`, from `core/jellyfin_api.py`.
  - New: `Library(id, name, kind, locations)`, `ServerUser(id, name, is_admin, is_disabled)` and `RecentItem`.
- **The next-episode helper**, rewritten against plain fields (season, episode, duration in ms) instead of plexapi attribute names. Jellyfin borrows `PlexManager._get_next_episodes` today.
- **The interface:**

```python
class MediaServer(Protocol):
    key: str                      # "plex" | "jellyfin", stored in trackers as `source`
    label: str                    # "Plex" | "Jellyfin"

    def connect(self) -> None: ...                     # raises ConnectionError
    def list_libraries(self) -> list[Library]: ...
    def list_users(self) -> list[ServerUser]: ...
    def get_on_deck(self, options: OnDeckOptions) -> list[OnDeckItem]: ...
    def get_watchlist(self, options: WatchlistOptions) -> list[WatchlistItem]: ...
    def get_active_sessions(self) -> ActiveSessions: ...   # paths in this server's path space
    def is_ondeck_complete(self) -> bool: ...
    def is_watchlist_complete(self) -> bool: ...
    # Phase 2
    def resolve_items(self, ids: list[str]) -> dict[str, list[str]]: ...   # pins, upgrade check
    def get_recently_added(self, options: RecentOptions) -> list[RecentItem]: ...
```

- **`PlexServer` adapter:** wraps today's `PlexManager`. RSS, plex.tv tokens, `switchHomeUser` and the plex_db fallback stay inside it, unchanged.
- **`JellyfinServer` adapter:** wraps `JellyfinManager` and drops the import of `core.plex_api`.
- **Settings that apply to both servers:** `number_episodes`, `days_to_monitor` and similar settings live on `PlexConfig` today and move to a server-neutral config. The settings keys don't change.

### 2. A run loops over the enabled servers

- **Connect:** `PlexCacheApp` holds `servers: list[MediaServer]` instead of `plex_manager` plus `jellyfin_manager`.
- **Changed call sites:** connect and init (`core/app.py:274, 861-862`), sessions and OnDeck (`:1082-1103`, `:1192-1208`), the completeness gates (`:1387-1411`, `:1550-1563`), watchlist and favorites (`:1591-1697`), and pins (`:1449-1458`).
- **Sessions:** the paths from every server, each converted with that server's mappings.
- **OnDeck and watchlist:** already merged by real path.
- **A server that can't be reached:**
  - If every enabled server fails, the run stops (as today).
  - If one of two fails, the run still caches what the other reports, but **skips move-back**, because it can't tell whether the missing server's users are still watching. That is today's Jellyfin rule (README, Jellyfin section) applied to both servers.

### 3. Settings and setup

- **Which servers are on:**
  - Keep the existing keys: `PLEX_URL`, `PLEX_TOKEN`, `valid_sections`, `users`, and `jellyfin_url` / `jellyfin_api_key` / ...
  - Add `plex_enabled` and `jellyfin_enabled`, so a server can be switched off without deleting its settings.
  - When the flag is missing, a server counts as enabled when it is configured, so existing installs load unchanged.
- **Validation:** at least one server must be enabled and configured. `PLEX_URL` / `PLEX_TOKEN`, `valid_sections` and `users_toggle` are only required when Plex is enabled.
- **`is_setup_complete()`:** at least one server is configured.
- **Credentials:** Jellyfin uses an API key (Jellyfin Dashboard > API Keys) where Plex uses a token. One admin key can read every user's Resume and Next Up, so Jellyfin needs no per-user tokens.
- **Setup wizard:**
  - Step 2 "Connect to Plex" becomes "Connect your media servers": a Plex card (today's flow, OAuth or URL) and a Jellyfin card (URL, API key, Test). At least one is needed.
  - Steps 3 and 4 list the libraries and users of each connected server.
- **Settings:**
  - The Plex tab becomes **Media Servers**, with Plex and Jellyfin sections (enabled, URL, credential, Test).
  - Libraries and Users are grouped by server.
  - Settings search gains the Jellyfin entries.
  - The `JELLYFIN_*` env vars keep winning over the settings file, as today.

### 4. Path mappings per server

Today a `PathMapping` (`core/config.py:44-72`) has one `plex_path`. Jellyfin paths reach it through `jellyfin_path_mappings`, so a Jellyfin path takes two hops to a real path: Jellyfin → Plex → real.

- **New fields:** `jellyfin_path` and `jellyfin_library_id` on `PathMapping`. `plex_path` and `section_id` stay. A library only Jellyfin has gets a mapping with an empty `plex_path`.
- **Conversion (done, `babe0cd`):** the run builds Jellyfin's translation from the enabled mappings (`PlexCacheApp._jellyfin_paths`). A Jellyfin path under `jellyfin_path` becomes the mapping's **`plex_path` when it has one**, so a file both servers report keeps one tracker entry, exactly as with `jellyfin_path_mappings` today; **otherwise its `real_path`**. `MultiPathModifier.convert_plex_to_real` already passes real paths through unchanged, so nothing downstream needed a per-server conversion.
  - This replaced the earlier plan of a per-server `to_real(path, server)`. It needs no change to `MultiPathModifier` and no tracker migration.
  - `jellyfin_path_mappings` still applies; a mapping's own `jellyfin_path` wins for the same prefix. Folders of disabled mappings are skipped without the "unmapped path" notice.
  - Web UI mapping edits keep `section_id`, `jellyfin_path` and `jellyfin_library_id` (`SettingsService.KEPT_MAPPING_FIELDS`).
- **Migration (with the Libraries screen):** each `jellyfin_path_mappings` entry (Jellyfin prefix → Plex prefix) folds into the mappings whose `plex_path` starts with that Plex prefix, as `jellyfin_path = jellyfin_prefix + rest`. Entries that line up with no mapping are kept and logged. One-time, marker in `data/migrations.json` as in #202, settings backed up first. Not needed for the run itself, since both forms work side by side; it's for showing one list in the UI.
- **Library toggles:** Jellyfin's library list (`/Library/VirtualFolders`: name, `ItemId`, `CollectionType`, `Locations`) feeds setup and the Libraries tab, the way Plex sections do. Jellyfin items whose path falls in a disabled mapping are skipped. That gives Jellyfin the library filter it doesn't have today.

### 5. IDs and trackers

- **Item IDs:** every item carries `source` plus its ID. Plex rating keys are numeric strings, and Jellyfin IDs are GUID strings. Stored entries keep the `rating_key` field name, so existing files load unchanged, and the ID is read together with `source`.
- **Pins** (`core/pinned_media.py`):
  - The pin tracker is keyed by rating key, and `plex_server.fetchItem(int(rk))` (`:296`) raises `ValueError` for a non-numeric ID. With `prune=True` that **deletes the pin** (`:310-316`).
  - Fix this first: resolve each pin through its own server, and never prune a pin whose server isn't connected or whose ID it can't read.
  - Pin keys become `"<source>:<id>"`, and existing keys are read as Plex.
- **Watchlist tracker:** keyed by **Plex path** (`core/app.py:1606`, `core/file_operations.py:1462-1472`). **No re-key in phase 1.** With section 4's conversion, a file keeps its Plex path whenever its mapping has one, so both servers still share entries; only a Jellyfin-only library's entries are keyed by real path, and nothing Plex-only reads those.
  - A real-path re-key stays possible later (the migration would convert keys with the current mappings and keep any it can't convert). The readers that build the Plex-path form would change with it: Recently Added (`web/services/recently_added_service.py:311`) and the upgrade check (`web/services/cache_service.py:2184-2187`).
- **Timestamps:** already carry `rating_key`, `media_type` and `source`, so they need no change.

### 6. Users

- **Listing:** Jellyfin users come from `/Users` (Id, Name, `Policy.IsAdministrator`, `Policy.IsDisabled`).
- **Settings:** they get the same per-user settings as Plex users (skip OnDeck, skip favorites, per-user retention days), stored in `users` with `server: "jellyfin"` and their Jellyfin ID. `jellyfin_skip_users` migrates into per-user skips.
- **OnDeck days:** the Jellyfin OnDeck fetch uses the per-user days, as Plex does (`core/app.py:1192-1205`). Today it gets only the global value.

### 7. Web features (phase 2)

| Feature | Plex | Jellyfin |
|---|---|---|
| Recently Added | `get_recently_added_media` (`core/plex_api.py`) | Done: `JellyfinManager.get_recently_added_media`, `/Items` per mapped movie or TV library sorted by `DateCreated` |
| Pins | `plex.search`, `fetchItem` (`web/services/pinned_service.py`) | Done: `/Items?searchTerm=`, `/Items?parentId=`, `/Items?ids=` with `MediaSources` for the version choice |
| Upgrade check | `fetchItem(rating_key)`, compare the file (`web/services/cache_service.py`) | Done: matched on a key Jellyfin keeps across a swap (`jellyfin_key`) |
| Duplicate scan | `library.sections()` (`web/services/duplicate_service.py`) | Done: library listings grouped by that key, plus versions within an item |
| Dashboard status | "Plex Connected" | Done: a badge per server in use |
| Source badges | done in #234 | done in #234 |

How the done ones behave:

- **Recently Added:** each server in use is asked, Plex first. A file both report is one row (Jellyfin paths are translated to the same form), Jellyfin's libraries are marked "(Jellyfin)" when both are in use, and a server that fails is named while the other's items still show. One version per item, by `pinned_preferred_resolution`: Jellyfin's `MediaSources` are adapted to `select_media_version`.
- **Pins:** a pin on a Jellyfin item is keyed by its Jellyfin id with `source: "jellyfin"`, and resolved by Jellyfin (movie or episode to its chosen version, show or season to its episodes). It is **never pruned**: Jellyfin derives a movie's or episode's id from its file path, so a file replaced by one with a new name gets a new id, and the old pin is logged as missing until the user unpins it. Show and season ids follow their folders and survive upgrades. A server the run doesn't use leaves its pins in place, unapplied; a server in use that can't be reached reads as unknown, never as "not pinned".
- **Upgrade check (question 5):** Jellyfin's item id changes with the file, so Jellyfin OnDeck entries record a `jellyfin_key` beside Plex's `rating_key`: what Jellyfin itself keys watch progress by, `jellyfin:movie:tmdb:<id>` (else IMDb) or `jellyfin:episode:<series id>:<season>:<episode>` (`core/jellyfin_api.upgrade_key`). The OnDeck tracker indexes both fields, so the run's swap detection works unchanged: the next run sees the new file under the same key (Jellyfin carries the progress over, so a swapped movie stays in Continue Watching). A file both servers follow is found under both keys and transferred once. The Maintenance audit asks Jellyfin which file answers to the key now (`files_for_upgrade_key`: an episode's series by season number; a movie's TMDb/IMDb id in a listing of the mapped movie libraries, read once per audit). If the old file still answers, it's not an upgrade; if several new files answer (a film kept in two copies), the ones already tracked are left out and only a single remaining file counts. For a file both servers report, Plex's answer stands. The folder idea in the original proposal didn't hold: a movie in its own folder is resolved from that folder (`MovieResolver`), so its `ParentId` is the library folder every movie shares.
- **Duplicate scan (question 6):** each server in use is scanned. Jellyfin's mapped movie and TV libraries are listed a page at a time (500) with `fields=ProviderIds,MediaSourceCount` and no images or user data; items are grouped by the same key within a library, and an item with `MediaSourceCount` > 1 (versions) counts on its own. Files (`MediaSources`, with their streams) are fetched only for those groups, 50 ids a request. The results use the Plex scan's multi-version check, Sonarr/Radarr orphan check, ignore list (keyed by the group key, which survives a re-scan) and delete step. A group with a file no mapping covers is left out, since it couldn't be deleted by a known path. With both servers, a Jellyfin group whose files Plex already listed is left out and Jellyfin's libraries are marked "(Jellyfin)". The card's "needs" state shows only when neither server is in use.

Checked against Jellyfin's source at the v10.9.0 and v10.10.0 tags (and `master`), Nov 16, 2026:

- **API key, no user:** `ItemsController.GetItems` accepts an API key without `userId` and then reads every library ("api keys can always access all folders"). Recently Added, pin search and pin resolution rely on this.
- **Item ids:** `LibraryManager.GetNewItemId` is the MD5 of the item's type name and path (lowercased unless case-sensitive ids are on). A movie's or episode's path is its file, so a replacement with a new name gets a new id; series and seasons are folders and keep theirs.
- **Seasons without a user:** `Season.GetItemsInternal` falls back to a folder query, so `/Items?parentId=<season>` misses episodes that sit directly in the series folder (a virtual season). `/Shows/{series}/Episodes?seasonId=` goes through `Series.GetSeasonEpisodes` and finds both, so pins use the TV endpoints (`099a25d`).
- **Fields:** `ItemFields` has `Path`, `MediaSources`, `DateCreated`, `ChildCount`, `ProviderIds`; `ProductionYear` is not one (always in the reply; unknown values are skipped by `CommaDelimitedCollectionModelBinder`). Each `MediaSources` entry carries its own `MediaStreams`, `Size` and `Bitrate`, which the version choice reads.
- **Versions:** an item has several `MediaSources` only for local alternate versions (files named for it, like "Movie - 1080p.mkv") or versions grouped by hand. Other copies of the same film are separate items.
- **No provider-id lookup:** `/Items` filters on whether an item has a TMDb/IMDb/TVDb id (`hasTmdbId`), not on its value.
- **What survives a swap:** watch progress is keyed by `BaseItem.GetUserDataKeys()`: a movie's IMDb and TMDb ids (`Video`), an episode's series keys with its season and episode numbers (`Episode`). `master` reads progress rows by those keys too (`UserDataManager.ResolveUserDataRow`). A movie's `Path` is its file and its `Name` starts as its folder name (`MovieResolver`), its parent being the library folder.
- **Listings:** `/Items` takes `startIndex`, `limit`, `enableImages`, `enableUserData`, and `fields=MediaSourceCount` (set only when an item has more than one source). `/Shows/{series}/Episodes?season=<number>` finds the season by number through `Series.GetSeasons`, without a user.

Still to check on a real server (Brandon can run one on Unraid): the whole flow end to end, `/Sessions` paths, and path substitution, which can rewrite `MediaSources` paths (`BaseItem.GetVersionInfo`).

### 8. Sign-in

- **Plex installs:** Plex sign-in stays as it is.
- **Without Plex:** the password login already works (`validate_password`, `web/services/auth_service.py:377`).
- **Phase 3 (done, `05c2eaa`):** Jellyfin sign-in via `POST /Users/AuthenticateByName`, allowed for any administrator of the Jellyfin server (`Policy.IsAdministrator`), as only the owner may with Plex. No admin id is stored: Jellyfin answers who is an administrator at each sign-in. The request carries the client, device, device id and version Jellyfin requires; the device id is the install's `plexcache_client_id`, so a new sign-in replaces its own device entry, and the token is logged out at once (`POST /Sessions/Logout`) so nothing stays in Jellyfin's Devices list. Settings > Security asks Plex for the owner only when Plex is in use.
- **API key (#236):** unaffected.

### 9. Wording

- **UI copy:** says "media server" where it means either one, and "Plex" only where it really is Plex-only.
- **Docs and template:** the Community Apps template, README and Unraid guide change their "Plex is still required" lines once Jellyfin-only ships.

## Compatibility rules

- Existing settings files load unchanged. Migrations are additive, run once, back up first, and leave a marker.
- A Plex-only install makes the same moves before and after each phase. Check this with runs on fixture settings compared before and after.
- The env vars, the settings import/export (`KNOWN_SETTINGS_KEYS`, `REDACTED_EXPORT_KEYS`) and the API key keep working. New secret settings join the export redaction.

## Phases

### Phase 1: Jellyfin-only installs work

- [x] Pins: never pruned for an ID the server can't read; pins of another server are skipped by the Plex resolver (`8ed7ef6`). Ship this early, on its own.
- [x] `core/media_server.py`: shared types and the `MediaServer` interface; Plex and Jellyfin adapters with no behaviour change, checked by a differential run against the previous commit (`fadc88d`).
- [x] `PlexCacheApp` loops over the servers for sessions, OnDeck, the watchlist and the move-back checks (`fadc88d`).
- [x] The shared next-episode helper moved into `core/media_server.py`, so `core/jellyfin_api.py` no longer imports `core.plex_api` (`192664e`).
- [ ] Still open from the interface: connecting through the interface; the one-server-down rule (open question 2).
- [x] Config: `plex_enabled` / `jellyfin_enabled`, at least one server, Plex-only fields required only for Plex, and setup completeness (`setup_has_media_server`); without Plex the run builds no Plex client and leaves pins alone (`b549a3a`).
- [x] Path mappings: `jellyfin_path` and `jellyfin_library_id`, Jellyfin paths to the mapping's Plex or real path (`babe0cd`).
- [x] Migration of `jellyfin_path_mappings` into the mappings, run when the Libraries tab loads the Jellyfin cards: backs up the settings first, removes only entries that covered a mapping, and changes nothing a second time (`b08ffd7`).
- [x] Jellyfin library list (`/Library/VirtualFolders`) and library toggles (`c821f8f`).
- [x] Setup wizard: "Use Jellyfin" on step 2, Jellyfin's libraries and users on steps 3 and 4, password login without Plex, and `/setup/complete` no longer saves the wizard's `_` keys (`4d64522`). Settings: the Media Servers tab (`8fa030c`), Libraries (`c821f8f`) and Users (`f659b4c`) grouped by server, with search entries.
- [x] "Needs Plex" states: the duplicate scan, and the upgrade check skipped with Plex off (`6df19d9`). Recently Added and pins support Jellyfin instead (phase 2).
- [x] Settings rewrite: every run rewrote `path_mappings` without the Jellyfin fields and turned an empty `plex_path` into `"/"`, which matches every path. Fixed with round-trip tests through `load_config()` (`2b85b9c`).
- [x] Tests:
  - [x] the wizard's Jellyfin-only settings load and validate (`tests/test_setup_jellyfin.py`)
  - [x] a run applies Jellyfin pins without Plex, and both servers' pins together (`tests/test_jellyfin_pins.py`)
  - [x] Plex-only rewrites of the settings file unchanged (`tests/test_jellyfin_path_mappings.py`)
  - [x] whole runs against a fake Jellyfin: Jellyfin only, both servers, and one server down either way round (the other's items are cached, move-back is held) (`tests/test_media_server_runs.py`)
  - [x] the migrations on settings shaped like v3.3.0 installs (Plex only, and with #234's Jellyfin keys) and on a wizard-made Jellyfin-only file; moving `jellyfin_path_mappings` changes where no Jellyfin file goes (`tests/test_media_server_migrations.py`)

### Phase 2: Jellyfin in the web features

- [x] Dashboard status per server, kept current after a server change (`dc0c553`, `8b32fd9`).
- [x] Recently Added (`ad97012`).
- [x] Pins: search, expand, versions, budget, the run, Recently Added and Cached Files (`f41362e`).
- [x] The upgrade check: the run and the Maintenance audit follow a Jellyfin file swap by `jellyfin_key` (`tests/test_jellyfin_upgrade_check.py`, and a two-run test in `tests/test_media_server_runs.py`).
- [x] The duplicate scan: Jellyfin-only and both servers (`tests/test_jellyfin_duplicate_scan.py`).

### Phase 3: Sign-in and wording

- [x] Jellyfin sign-in, and login switched on without Plex in Settings > Security and the wizard (`05c2eaa`).
- [x] UI copy: "media server" where a setting covers both (`2f76cbb`); Plex-only features say so.
- [x] Community Apps template, README and Unraid guide (`ed00eed`). The template's `<Changes>` waits for the release.
- [x] `JELLYFIN_PATH_MAPPINGS` (the env var) is folded onto the mappings too, so CA installs see their libraries as on (`eaaf828`).

### Proposed upstream PRs

Phase 1 is too big for one review. Proposed split, each PR passing alone and leaving Plex installs unchanged:

1. The pin safety fix.
2. The interface and adapters, a refactor with no behaviour change.
3. Config, validation and setup completeness.
4. Path mappings, the watchlist re-key and their migrations.
5. Setup and Settings screens.

## Open questions

1. **Name.** "PlexCache-D" with Jellyfin-only installs: keep it, or rename? StudioNirin's call.
2. **One server down.** Cache from the other and skip move-back (proposed), or stop the run as today?
3. **Jellyfin library filter.** By mapping (proposed), or by library ID on each item (`ParentId` / ancestors)?
4. **Emby.** It shares Jellyfin's API ancestry. Out of scope unless someone asks.
5. **Jellyfin upgrade check.** Settled and built (section 7): matched on what Jellyfin keys watch progress by.
6. **Jellyfin duplicate scan.** Settled and built (section 7): library listings grouped by the same key.

## Reference

`core/app.py` call sites that change: `:202` (config load), `:274` and `:861-862` (connect and init), `:1082-1153` (sessions), `:1192-1240` (OnDeck), `:1387-1411` (move-back gates), `:1413-1548` (pins), `:1550-1741` (watchlist and favorites).

Web consumers of Plex:

| Area | Where |
|---|---|
| Settings and setup | `SettingsService.get_plex_libraries` / `get_plex_users` (`web/services/settings_service.py:1125-1349`), `web/routers/setup.py:76-428` |
| Recently Added | `RecentlyAddedService._connect_plex` (`web/services/recently_added_service.py:130-205`) |
| Pins | `web/services/pinned_service.py:60-858` |
| Duplicate scan | `DuplicateService.scan_plex_libraries` (`web/services/duplicate_service.py:205-310`) |
| Upgrade check | `CacheService.check_for_upgrades` (`web/services/cache_service.py:2007-2190`) |
| Sign-in | `AuthService.validate_plex_login` (`web/services/auth_service.py:285-317`) |
