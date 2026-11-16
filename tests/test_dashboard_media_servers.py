"""Dashboard status: one badge per media server in use (docs/media-servers-design.md).

The status card said "Plex Connected" / "Plex Not Connected" whatever the
install used, so a Jellyfin-only install always showed Plex as down. Now
SettingsService.media_server_status() lists the servers the run uses, Plex
first, each with whether it answers, and the card shows a badge for each.
GET /api/status keeps "plex" and adds "media_servers".
"""

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from jinja2 import Environment, FileSystemLoader

sys.modules.setdefault('fcntl', MagicMock())
for _mod in ['apscheduler', 'apscheduler.schedulers', 'apscheduler.schedulers.background',
             'apscheduler.triggers', 'apscheduler.triggers.cron', 'apscheduler.triggers.interval',
             'plexapi', 'plexapi.server']:
    sys.modules.setdefault(_mod, MagicMock())

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "web" / "templates"
PLEX = {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "tok"}
JELLYFIN = {"jellyfin_enabled": True, "jellyfin_url": "http://jf:8096", "jellyfin_api_key": "key"}


@pytest.fixture(autouse=True)
def _no_jellyfin_env(monkeypatch):
    for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def service(tmp_path):
    started = []

    def make(settings):
        settings_file = tmp_path / "plexcache_settings.json"
        settings_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        for p in (patch("web.services.settings_service.SETTINGS_FILE", settings_file),
                  patch("web.services.settings_service.DATA_DIR", tmp_path)):
            p.start(); started.append(p)
        from web.services.settings_service import SettingsService
        svc = SettingsService()
        svc.check_plex_connection = MagicMock(return_value=True)
        svc.check_jellyfin_connection = MagicMock(return_value=False)
        return svc

    yield make
    for p in reversed(started):
        p.stop()


@pytest.mark.parametrize("settings, expected", [
    (PLEX, [("plex", True)]),
    ({**PLEX, **JELLYFIN}, [("plex", True), ("jellyfin", False)]),
    ({**JELLYFIN, "plex_enabled": False}, [("jellyfin", False)]),
    ({**PLEX, **JELLYFIN, "plex_enabled": False}, [("jellyfin", False)]),
    ({}, []),
])
def test_status_lists_the_servers_the_run_uses(service, settings, expected):
    svc = service(settings)
    assert [(s["key"], s["connected"]) for s in svc.media_server_status()] == expected
    if not any(key == "plex" for key, _ in expected):
        svc.check_plex_connection.assert_not_called()


def test_jellyfin_check_uses_the_saved_key(service):
    svc = service({**JELLYFIN, "jellyfin_url": "jf:8096"})
    from web.services.settings_service import SettingsService
    with patch("requests.get") as get:
        get.return_value.status_code = 200
        assert SettingsService.check_jellyfin_connection(svc) is True
    url = get.call_args.args[0]
    assert url == "http://jf:8096/System/Info" and 'Token="key"' in get.call_args.kwargs["headers"]["Authorization"]
    import requests
    with patch("requests.get", side_effect=requests.ConnectionError("down")):
        assert SettingsService.check_jellyfin_connection(svc) is False


def test_jellyfin_check_without_a_key_asks_nothing(service):
    svc = service({"jellyfin_url": "http://jf:8096"})
    from web.services.settings_service import SettingsService
    with patch("requests.get") as get:
        assert SettingsService.check_jellyfin_connection(svc) is False
    get.assert_not_called()


def _badges(stats_extra):
    stats = {
        "cache_files": 0, "cached_files_size": None, "associated_files_count": 0, "cache_limit_exceeded": False,
        "min_free_space_warning": False, "cache_limit_approaching": False, "usage_percent": 0,
        "configured_limit_percent": 0, "configured_limit_display": None, "cache_size": "0 B", "cache_limit": "0 B",
        "eviction_threshold_display": None, "eviction_over_threshold": False, "eviction_over_by_display": None,
        "ondeck_count": 0, "ondeck_tracked_count": 0, "watchlist_count": 0, "watchlist_tracked_count": 0,
        "health_issues": 0, "health_warnings": 0, "health_orphaned_count": 0, "health_stale_exclude_count": 0,
        "health_stale_timestamp_count": 0, "last_run": "Never", "last_run_summary": None,
        **stats_extra,
    }
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    env.globals["static_url"] = lambda path: "/static/" + path
    html = env.get_template("partials/dashboard_stats.html").render(stats=stats)
    row = html.split('media-server-badges', 1)[1].split('</div>', 1)[0]
    return [" ".join(b.split()) for b in re.findall(r'<span class="badge [^"]*">\s*<i[^>]*></i>(.*?)</span>', row, re.S)]


def test_card_shows_a_badge_per_server():
    servers = [{"key": "plex", "label": "Plex", "connected": True},
               {"key": "jellyfin", "label": "Jellyfin", "connected": False}]
    assert _badges({"media_servers": servers, "plex_connected": True}) == ["Plex Connected", "Jellyfin Not Connected"]
    assert _badges({"media_servers": servers[1:], "plex_connected": False}) == ["Jellyfin Not Connected"]


def test_card_without_the_list_keeps_the_plex_badge():
    assert _badges({"plex_connected": True}) == ["Plex Connected"]
    assert _badges({"media_servers": [], "plex_connected": False}) == ["Plex Not Connected"]
