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
| Upgrade check | `fetchItem(rating_key)`, compare the file (`web/services/cache_service.py`) | Open (question 5). Skipped when Plex is off |
| Duplicate scan | `library.sections()` (`web/services/duplicate_service.py`) | Open (question 6). The card says it needs Plex |
| Dashboard status | "Plex Connected" | Done: a badge per server in use |
| Source badges | done in #234 | done in #234 |

How the done ones behave:

- **Recently Added:** each server in use is asked, Plex first. A file both report is one row (Jellyfin paths are translated to the same form), Jellyfin's libraries are marked "(Jellyfin)" when both are in use, and a server that fails is named while the other's items still show. One version per item, by `pinned_preferred_resolution`: Jellyfin's `MediaSources` are adapted to `select_media_version`.
- **Pins:** a pin on a Jellyfin item is keyed by its Jellyfin id with `source: "jellyfin"`, and resolved by Jellyfin (movie or episode to its chosen version, show or season to its episodes). It is **never pruned**: Jellyfin derives a movie's or episode's id from its file path, so a file replaced by one with a new name gets a new id, and the old pin is logged as missing until the user unpins it. Show and season ids follow their folders and survive upgrades. A server the run doesn't use leaves its pins in place, unapplied; a server in use that can't be reached reads as unknown, never as "not pinned".

Jellyfin endpoints and fields are checked against a fake Jellyfin in the tests, and still need a check against a real Jellyfin 10.9+ server, the minimum version since #234. In particular `/Items` without `userId` (Recently Added, pin search) relies on the admin API key.

### 8. Sign-in

- **Plex installs:** Plex sign-in stays as it is.
- **Without Plex:** the password login already works (`validate_password`, `web/services/auth_service.py:377`).
- **Phase 3:** Jellyfin sign-in via `POST /Users/AuthenticateByName`, allowed for Jellyfin administrators, with the admin stored as `auth_admin_jellyfin_id`.
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
- [ ] Tests:
  - [x] the wizard's Jellyfin-only settings load and validate (`tests/test_setup_jellyfin.py`)
  - [x] a run applies Jellyfin pins without Plex, and both servers' pins together (`tests/test_jellyfin_pins.py`)
  - [x] Plex-only rewrites of the settings file unchanged (`tests/test_jellyfin_path_mappings.py`)
  - [ ] a whole Jellyfin-only run against a fake Jellyfin as a checked-in test (done so far with the scratch differential harness)
  - [ ] one server down
  - [ ] every migration on real settings files

### Phase 2: Jellyfin in the web features

- [x] Dashboard status per server, kept current after a server change (`dc0c553`, `8b32fd9`).
- [x] Recently Added (`ad97012`).
- [x] Pins: search, expand, versions, budget, the run, Recently Added and Cached Files (`f41362e`).
- [ ] The upgrade check (open question 5).
- [ ] The duplicate scan (open question 6).

### Phase 3: Sign-in and wording

- [ ] Jellyfin sign-in.
- [ ] UI copy.
- [ ] Community Apps template, README and Unraid guide.

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
5. **Jellyfin upgrade check.** Plex keeps an item's rating key when Sonarr or Radarr swaps its file; Jellyfin gives a movie or episode a new id. Proposed: record what survives a swap on Jellyfin's tracker entries (the series id with season and episode numbers for an episode, the folder for a movie) and look the new file up by that. Needs checking against a real Jellyfin first.
6. **Jellyfin duplicate scan.** Jellyfin groups versions only when the files follow its naming ("Movie - 1080p.mkv"); other copies show up as separate items. Scan `MediaSources` for grouped versions only, or also match separate items by provider id? The scan offers to delete files, so it should not guess.

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
