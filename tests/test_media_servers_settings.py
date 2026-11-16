"""Settings > Media Servers: Plex and Jellyfin side by side (docs/media-servers-design.md).

The Plex tab becomes Media Servers. Plex gets a "Use Plex" switch, and a
Jellyfin card sets the URL, API key, whether the run uses Jellyfin, and
favorites. Neither switch can leave the run without a media server. Test
Connection checks what's typed, so it works before saving. A URL typed
without http:// gets it, here and when the run reads the settings.

Mounts only the settings router on a minimal app, backed by a real
SettingsService over a temp settings file, as tests/test_custom_path_mappings.py.
"""

import html
import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.modules.setdefault('fcntl', MagicMock())
for _mod in ['apscheduler', 'apscheduler.schedulers', 'apscheduler.schedulers.background',
             'apscheduler.triggers', 'apscheduler.triggers.cron', 'apscheduler.triggers.interval',
             'plexapi', 'plexapi.server']:
    sys.modules.setdefault(_mod, MagicMock())

from core.config import normalize_jellyfin_url  # noqa: E402

PLEX = {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "tok", "valid_sections": [1]}


@pytest.fixture(autouse=True)
def _no_jellyfin_env(monkeypatch):
    for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def make_client(tmp_path):
    patches = []

    def client(settings):
        settings_file = tmp_path / "plexcache_settings.json"
        settings_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        for p in (patch("web.services.settings_service.SETTINGS_FILE", settings_file),
                  patch("web.services.settings_service.DATA_DIR", tmp_path)):
            p.start(); patches.append(p)
        from web.services.settings_service import SettingsService
        svc = SettingsService()
        svc._cached_settings = None
        from web.routers import settings as settings_router
        p = patch("web.routers.settings.get_settings_service", return_value=svc)
        p.start(); patches.append(p)
        app = FastAPI()
        app.include_router(settings_router.router, prefix="/settings")
        return TestClient(app), svc

    yield client
    for p in reversed(patches):
        p.stop()


def _jellyfin_form(**fields):
    form = {"jellyfin_url": "192.168.1.10:8096", "jellyfin_api_key": "key", "jellyfin_enabled": "true"}
    form.update(fields)
    return {k: v for k, v in form.items() if v is not None}


@pytest.mark.parametrize("typed, saved", [
    ("192.168.1.10:8096", "http://192.168.1.10:8096"),
    ("https://jf.example.com/", "https://jf.example.com"),
    ("  http://jf:8096  ", "http://jf:8096"),
    ("", ""),
])
def test_normalize_jellyfin_url(typed, saved):
    assert normalize_jellyfin_url(typed) == saved


def test_the_tab_shows_both_servers(make_client):
    client, _ = make_client(PLEX)
    page = client.get("/settings/plex").text
    assert "Media Servers" in page and ">Plex<" in page and ">Jellyfin<" in page
    assert 'name="plex_enabled" value="true" checked' in page
    assert 'name="jellyfin_enabled" value="true" >' in page or 'name="jellyfin_enabled" value="true">' in page


def test_saving_jellyfin(make_client):
    client, svc = make_client(PLEX)
    response = client.put("/settings/jellyfin", data=_jellyfin_form(jellyfin_favorites_toggle="true"))
    assert "Jellyfin settings saved" in response.text
    raw = svc._load_raw()
    assert raw["jellyfin_url"] == "http://192.168.1.10:8096"
    assert raw["jellyfin_api_key"] == "key" and raw["jellyfin_enabled"] is True
    assert raw["jellyfin_favorites_toggle"] is True


def test_using_jellyfin_needs_url_and_key(make_client):
    client, svc = make_client(PLEX)
    response = client.put("/settings/jellyfin", data=_jellyfin_form(jellyfin_api_key=""))
    assert "Enter the Jellyfin URL and API key" in response.text
    assert "jellyfin_enabled" not in svc._load_raw()


def test_plex_can_go_off_once_jellyfin_is_set_up(make_client):
    client, svc = make_client(PLEX)
    refused = client.put("/settings/plex", data={"plex_url": PLEX["PLEX_URL"], "plex_token": "tok"})
    assert "only media server" in refused.text and "plex_enabled" not in svc._load_raw()

    client.put("/settings/jellyfin", data=_jellyfin_form())
    client.put("/settings/plex", data={"plex_url": PLEX["PLEX_URL"], "plex_token": "tok"})
    raw = svc._load_raw()
    assert raw["plex_enabled"] is False and raw["PLEX_URL"] == PLEX["PLEX_URL"]


def test_the_last_server_cant_be_switched_off(make_client):
    client, svc = make_client({**PLEX, "plex_enabled": False, "jellyfin_url": "http://jf:8096",
                               "jellyfin_api_key": "key", "jellyfin_enabled": True})
    response = client.put("/settings/jellyfin", data=_jellyfin_form(jellyfin_enabled=None))
    assert "only media server in use" in response.text
    assert svc._load_raw()["jellyfin_enabled"] is True


def test_env_vars_win_and_are_left_alone(make_client, monkeypatch):
    monkeypatch.setenv("JELLYFIN_URL", "http://env-jf:8096")
    monkeypatch.setenv("JELLYFIN_API_KEY", "env-key")
    client, svc = make_client(PLEX)
    page = client.get("/settings/plex").text
    assert "Set by the container's JELLYFIN_URL variable" in page
    client.put("/settings/jellyfin", data=_jellyfin_form(jellyfin_url="http://typed", jellyfin_api_key="typed"))
    raw = svc._load_raw()
    assert "jellyfin_url" not in raw and "jellyfin_api_key" not in raw and raw["jellyfin_enabled"] is True


def test_test_connection_uses_the_typed_values(make_client):
    client, svc = make_client(PLEX)
    fake = MagicMock()
    fake.url = "http://192.168.1.10:8096"
    fake.get_server_info.return_value = {"ServerName": "Den", "Version": "10.10.3"}
    fake.list_libraries.return_value = [{"id": "1"}, {"id": "2"}]
    with patch.object(svc, "jellyfin_client", return_value=fake) as made:
        response = client.post("/settings/jellyfin/test", data=_jellyfin_form())
    made.assert_called_once_with("192.168.1.10:8096", "key")
    assert "Connected to 'Den' (Jellyfin 10.10.3), 2 libraries" in html.unescape(response.text)


def test_test_connection_explains_a_refused_key(make_client):
    import requests
    client, svc = make_client(PLEX)
    fake = MagicMock()
    error = requests.HTTPError("401")
    error.response = MagicMock(status_code=401)
    fake.get_server_info.side_effect = error
    with patch.object(svc, "jellyfin_client", return_value=fake):
        response = client.post("/settings/jellyfin/test", data=_jellyfin_form())
    assert "refused the API key" in response.text


def test_the_run_reads_a_url_without_a_scheme(monkeypatch, tmp_path):
    from core.config import ConfigManager
    config = ConfigManager(str(tmp_path / "s.json"))
    config.settings_data = {"jellyfin_url": "192.168.1.10:8096", "jellyfin_api_key": "key"}
    config._load_jellyfin_config()
    assert config.jellyfin.url == "http://192.168.1.10:8096"
