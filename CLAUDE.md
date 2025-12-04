# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PlexCache-R is a Python automation tool for Plex media management on Unraid. It caches frequently-accessed media (OnDeck/Watchlist) to a fast cache drive and returns watched media to the main array. This minimizes array spinup cycles and reduces energy consumption.

## Commands

```bash
# Install dependencies
pip3 install -r requirements.txt

# Interactive setup (creates plexcache_settings.json)
python3 plexcache_setup.py

# Run the application
python3 plexcache_app.py

# Common flags
python3 plexcache_app.py --dry-run           # Simulate without moving files
python3 plexcache_app.py --verbose           # DEBUG level logging
python3 plexcache_app.py --skip-cache        # Bypass cache, fetch fresh API data
python3 plexcache_app.py --quiet             # Only notify on errors
python3 plexcache_app.py --restore-plexcached # Emergency restore .plexcached files

# Diagnostics
python3 audit_cache.py                       # Compare cache vs exclude list vs timestamps
```

## Architecture

```
plexcache_app.py (Main Orchestrator)
    ├── config.py          - Configuration management (dataclasses, JSON settings)
    ├── logging_config.py  - Logging, rotation, Unraid/webhook notification handlers
    ├── system_utils.py    - OS detection, path conversions, file utilities
    ├── plex_api.py        - Plex server interactions, caching (watchlist, watched status)
    └── file_operations.py - File moving, filtering, subtitles, timestamp tracking
```

**Key Classes in file_operations.py:**
- `FilePathModifier` - Converts Plex paths to real filesystem paths
- `FileMover` - Concurrent file moving with configurable thread pool
- `CacheTimestampTracker` - Thread-safe JSON tracking of when files were cached
- `WatchlistTracker` - Tracks retention periods for watchlist items
- `PlexcachedRestorer` - Emergency restore of .plexcached backup files

**Key Classes in plex_api.py:**
- `PlexManager` - Main Plex server interactions (OnDeck, Watchlist, Watched status)
- `CacheManager` - JSON file caching for API responses
- `UserTokenCache` - In-memory + disk token cache with expiry

## Data Files

- `plexcache_settings.json` - User configuration (created by setup.py)
- `plexcache_mover_files_to_exclude.txt` - Unraid exclude list
- `plexcache_timestamps.json` - When files were cached
- `plexcache_watchlist_cache.json` / `plexcache_watched_cache.json` - Cached API responses
- `logs/plexcache.log` - Rotating log (10MB, 5 backups)

## Key Design Patterns

**.plexcached Backup System:** When moving files to cache, array files are renamed to `filename.plexcached` (not deleted). This allows recovery if the cache drive fails. The `--restore-plexcached` flag performs emergency restoration.

**Remote User Support:** Local users get full OnDeck + Watchlist via API. Remote users get OnDeck via API but Watchlist via RSS feed fallback (all-or-nothing for remote watchlists).

**Retention Periods:** Cache retention (hours) prevents moving files that are actively being watched. Watchlist retention (days) auto-expires cached items.

## Debugging

```bash
python3 plexcache_app.py --verbose --dry-run    # Full debug output, no file moves
tail -f logs/plexcache.log                      # Watch logs in real-time
python3 audit_cache.py                          # Diagnose orphaned entries
```

## Git Commit Guidelines

- Never include "Generated with Claude Code" or similar AI attribution in commit messages
- Never include "Co-Authored-By: Claude" in commits
- Keep commit messages concise and focused on what changed
