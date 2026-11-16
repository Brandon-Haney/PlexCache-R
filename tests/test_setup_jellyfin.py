"""The setup wizard with Jellyfin as the only media server (docs/media-servers-design.md).

Step 2 offers "Use Jellyfin": it checks the URL and API key against Jellyfin,
then steps 3 and 4 list Jellyfin's libraries and users. Library folders get
Jellyfin-only path mappings, unticked users become jellyfin_skip_users, and
login (if switched on) uses a username and password, since there's no Plex
sign-in. The settings the wizard saves load and validate as a Jellyfin-only
install, and the wizard's own working keys (starting with "_") are not saved.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.modules.setdefault('fcntl', MagicMock())
for _mod in ['apscheduler', 'apscheduler.schedulers', 'apscheduler.schedulers.background',
             'apscheduler.triggers', 'apscheduler.triggers.cron', 'apscheduler.triggers.interval',
             'plexapi', 'plexapi.server', 'plexapi.myplex']:
    sys.modules.setdefault(_mod, MagicMock())

LIBRARIES = [
    {"id": "lib-movies", "title": "Movies", "type": "movie", "type_label": "Movies", "locations": ["/media/movies"]},
    {"id": "lib-anime", "title": "Anime", "type": "show", "type_label": "TV Shows", "locations": ["/media/anime/"]},
]
USERS = [
    {"id": "u1", "name": "roger", "is_admin": True, "is_disabled": False},
    {"id": "u2", "name": "mia", "is_admin": False, "is_disabled": False},
]


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_SKIP_USERS"):
        monkeypatch.delenv(key, raising=False)
    settings_file = tmp_path / "plexcache_settings.json"
    settings_file.write_text("{}", encoding="utf-8")
    patches = [patch("web.services.settings_service.SETTINGS_FILE", settings_file),
               patch("web.services.settings_service.DATA_DIR", tmp_path)]
    for p in patches:
        p.start()
    from web.services.settings_service import SettingsService
    svc = SettingsService()
    from web.routers import setup as setup_router
    setup_router.clear_setup_state()
    fake = MagicMock()
    fake.get_server_info.return_value = {"ServerName": "Den", "Version": "10.10.3"}
    fake.list_libraries.return_value = LIBRARIES
    fake.list_users.return_value = USERS
    patches += [patch("web.routers.setup.get_settings_service", return_value=svc),
                patch("web.routers.setup._jellyfin_setup_client", return_value=fake)]
    for p in patches[2:]:
        p.start()
    app = FastAPI()
    app.include_router(setup_router.router)
    client = TestClient(app, follow_redirects=False)
    yield client, svc, settings_file, fake
    setup_router.clear_setup_state()
    for p in reversed(patches):
        p.stop()


def _through_step5(client):
    assert client.post("/setup/step2/jellyfin", data={"jellyfin_url": "192.168.1.10:8096",
                                                      "jellyfin_api_key": "key"}).status_code == 303
    client.post("/setup/step3", data={"libraries": ["lib-movies", "lib-anime"], "library_cacheable_lib-movies": "on",
                                      "cache_dir": "/mnt/cache"})
    client.post("/setup/step4", data={"cached_user": ["roger"]})
    client.post("/setup/step5", data={"number_episodes": "5", "days_to_monitor": "99", "watchlist_toggle": "on",
                                      "watchlist_episodes": "3", "watched_move": "on"})


def test_step2_offers_jellyfin(wizard):
    client, *_ = wizard
    assert "Use Jellyfin" in client.get("/setup?step=2").text
    page = client.get("/setup?step=2&server=jellyfin").text
    assert 'action="/setup/step2/jellyfin"' in page and 'id="plex-form"' not in page


def test_a_refused_key_stays_on_step2(wizard):
    import requests
    client, _, _, fake = wizard
    error = requests.HTTPError("401")
    error.response = MagicMock(status_code=401)
    fake.get_server_info.side_effect = error
    response = client.post("/setup/step2/jellyfin", data={"jellyfin_url": "jf:8096", "jellyfin_api_key": "bad"})
    assert response.status_code == 200 and "refused the API key" in response.text


def test_steps_3_and_4_show_jellyfins_libraries_and_users(wizard):
    client, *_ = wizard
    client.post("/setup/step2/jellyfin", data={"jellyfin_url": "jf:8096", "jellyfin_api_key": "key"})
    step3 = client.get("/setup?step=3").text
    assert 'value="lib-movies"' in step3 and 'value="lib-anime"' in step3
    assert 'onclick="addPathMapping()"' not in step3  # no manual Plex-path mappings for Jellyfin
    client.post("/setup/step3", data={"libraries": ["lib-movies"], "cache_dir": "/mnt/cache"})
    step4 = client.get("/setup?step=4").text
    assert "Jellyfin Users" in step4 and 'value="roger"' in step4 and 'value="mia"' in step4


def test_login_without_plex_needs_a_password(wizard):
    client, *_ = wizard
    _through_step5(client)
    refused = client.post("/setup/step6", data={"auth_enabled": "on"})
    assert refused.status_code == 200 and "username and password" in refused.text
    accepted = client.post("/setup/step6", data={"auth_enabled": "on", "auth_password_username": "admin",
                                                "auth_password": "s3cret"})
    assert accepted.status_code == 303


@pytest.mark.critical
def test_the_wizard_saves_a_jellyfin_only_install_that_loads(wizard, tmp_path):
    client, svc, settings_file, _ = wizard
    _through_step5(client)
    client.post("/setup/step6", data={})
    assert "Jellyfin Server" in client.get("/setup?step=7").text
    client.post("/setup/complete")

    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert not [key for key in saved if key.startswith("_")]
    assert saved["plex_enabled"] is False and saved["jellyfin_enabled"] is True
    assert saved["jellyfin_url"] == "http://192.168.1.10:8096" and saved["jellyfin_skip_users"] == ["mia"]
    movies, anime = saved["path_mappings"]
    assert (movies["jellyfin_path"], movies["plex_path"], movies["real_path"], movies["cache_path"]) == (
        "/media/movies", "", "/mnt/user/movies/", "/mnt/cache/movies/")
    assert anime["jellyfin_library_id"] == "lib-anime" and anime["cacheable"] is False and anime["cache_path"] is None

    from core.config import ConfigManager, setup_has_media_server
    assert setup_has_media_server(saved)
    config = ConfigManager(str(settings_file))
    config.load_config(persist_updates=False)
    assert not config.plex.enabled and config.jellyfin.enabled
    assert [m.jellyfin_path for m in config.paths.path_mappings] == ["/media/movies", "/media/anime"]


def test_choosing_plex_again_switches_back(wizard):
    client, *_ = wizard
    client.post("/setup/step2/jellyfin", data={"jellyfin_url": "jf:8096", "jellyfin_api_key": "key"})
    with patch("plexapi.server.PlexServer") as plex:
        plex.return_value.friendlyName = "Plex"
        client.post("/setup/step2", data={"plex_url": "http://plex:32400", "plex_token": "tok"})
    from web.routers.setup import _setup_state
    assert _setup_state["_setup_server"] == "plex" and _setup_state["plex_enabled"] is True
    assert "worked out from Jellyfin" not in client.get("/setup?step=3").text
