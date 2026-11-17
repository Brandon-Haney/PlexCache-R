"""What each server's absence switches off (docs/media-servers-design.md).

The duplicate scan reads Plex or Jellyfin: only with neither in use does its
card say so instead of offering a scan, and the scan itself refuse. With Plex
switched off, neither the scan nor the upgrade check connects to the Plex it
still has settings for (tests/test_jellyfin_duplicate_scan.py and
tests/test_jellyfin_upgrade_check.py cover Jellyfin's side).
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules.setdefault('fcntl', MagicMock())
for _mod in ['apscheduler', 'apscheduler.schedulers', 'apscheduler.schedulers.background',
             'apscheduler.triggers', 'apscheduler.triggers.cron', 'apscheduler.triggers.interval',
             'plexapi', 'plexapi.server']:
    sys.modules.setdefault(_mod, MagicMock())

from jinja2 import Environment, FileSystemLoader  # noqa: E402

TEMPLATES = Path(__file__).resolve().parent.parent / "web" / "templates"
PLEX_OFF = {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "tok", "plex_enabled": False,
            "jellyfin_enabled": True, "jellyfin_url": "http://jf:8096", "jellyfin_api_key": "key"}


def _card(**context):
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    return env.get_template("maintenance/partials/duplicate_card.html").render(
        scan_results=None, arr_configured=False, ignored_count=0, ignored_items=[], show_ignored=False, **context)


def test_the_duplicate_card_needs_a_media_server():
    html = _card(media_server_in_use=False)
    assert "Needs a Media Server" in html and "startDuplicateScan()" not in html
    assert "startDuplicateScan()" in _card(media_server_in_use=True)
    assert "startDuplicateScan()" in _card()  # rendered without the flag, as before


def test_the_duplicate_scan_refuses_without_a_media_server(tmp_path, monkeypatch):
    for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    settings_file = tmp_path / "plexcache_settings.json"
    settings_file.write_text(json.dumps({**PLEX_OFF, "jellyfin_enabled": False}), encoding="utf-8")
    from web.services.duplicate_service import DuplicateService
    service = DuplicateService.__new__(DuplicateService)
    with patch("web.config.SETTINGS_FILE", settings_file), patch("plexapi.server.PlexServer") as plex:
        result = service.scan_libraries()
    assert not result.success and "neither is in use" in result.message
    plex.assert_not_called()


def test_the_upgrade_check_leaves_a_switched_off_plex_alone():
    from web.services.cache_service import CacheService
    service = CacheService.__new__(CacheService)
    service._load_settings = lambda: {**PLEX_OFF, "path_mappings": []}
    service.get_ondeck_tracker = lambda: {"/mnt/user/movies/Old.mkv": {"rating_key": "101"}}
    service._cache_to_real = lambda cache_path, mappings: "/mnt/user/movies/Old.mkv"
    with patch("plexapi.server.PlexServer") as plex:
        result = service.check_for_upgrades(["/mnt/cache/movies/Old.mkv"])
    assert result == {"upgrades_found": 0, "upgrades_resolved": 0, "details": []}
    plex.assert_not_called()
