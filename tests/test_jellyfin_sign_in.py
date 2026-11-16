"""Signing in with Jellyfin (docs/media-servers-design.md, phase 3).

Administrators of the Jellyfin server sign in with their Jellyfin account:
PlexCache-D sends it to POST /Users/AuthenticateByName with the client and
device Jellyfin requires (SessionManager.AuthenticateNewSessionInternal),
accepts only Policy.IsAdministrator, and logs the new token out at once so
nothing stays in Jellyfin's Devices list. The login page offers the
sign-ins in use, and Settings > Security can switch login on without Plex.
"""

import json
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.modules.setdefault('fcntl', MagicMock())
for _mod in ['apscheduler', 'apscheduler.schedulers', 'apscheduler.schedulers.background',
             'apscheduler.triggers', 'apscheduler.triggers.cron', 'apscheduler.triggers.interval',
             'plexapi', 'plexapi.server', 'plexapi.myplex']:
    sys.modules.setdefault(_mod, MagicMock())

JELLYFIN = {"jellyfin_url": "jf.local:8096", "jellyfin_api_key": "key", "jellyfin_enabled": True,
            "plexcache_client_id": "install-1"}
JELLYFIN_ONLY = {**JELLYFIN, "plex_enabled": False}
PLEX = {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "tok"}


@pytest.fixture(autouse=True)
def _no_jellyfin_env(monkeypatch):
    for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def auth(tmp_path):
    settings_file = tmp_path / "plexcache_settings.json"

    def make(settings):
        settings_file.write_text(json.dumps({"auth_enabled": True, **settings}, indent=2), encoding="utf-8")
        from web.services.auth_service import AuthService
        return AuthService()

    with patch("web.services.auth_service.SETTINGS_FILE", settings_file), \
            patch("web.services.auth_service.DATA_DIR", tmp_path), \
            patch("web.services.auth_service.AuthService.SESSIONS_FILE", str(tmp_path / "sessions.json")):
        yield make


def _jellyfin_answers(status=200, admin=True, name="roger"):
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        response = MagicMock(status_code=200)
        if url.endswith("/Users/AuthenticateByName"):
            response.status_code = status
            response.json.return_value = {"AccessToken": "tok-123", "User": {
                "Id": "u-roger", "Name": name, "Policy": {"IsAdministrator": admin}}}
        return response

    return post, calls


def test_an_administrator_signs_in_and_the_token_is_logged_out(auth):
    service = auth(JELLYFIN_ONLY)
    post, calls = _jellyfin_answers()
    with patch("requests.post", side_effect=post):
        account, error = service.validate_jellyfin_login("roger", "pw")
    assert (account, error) == ({"account_id": "jellyfin:u-roger", "username": "roger"}, "")
    (login_url, body, login_headers), (logout_url, _, logout_headers) = calls
    assert login_url == "http://jf.local:8096/Users/AuthenticateByName"
    assert body == {"Username": "roger", "Pw": "pw"}
    # Jellyfin refuses a sign-in without these (ArgumentException.ThrowIfNullOrEmpty)
    fields = dict(re.findall(r'(\w+)="([^"]*)"', login_headers["Authorization"]))
    assert fields["Client"] == "PlexCache-D" and fields["Device"] == "PlexCache-D"
    assert fields["DeviceId"] == "install-1" and fields["Version"]
    assert logout_url == "http://jf.local:8096/Sessions/Logout"
    assert 'Token="tok-123"' in logout_headers["Authorization"]


def test_only_administrators_may_sign_in(auth):
    post, calls = _jellyfin_answers(admin=False, name="mia")
    with patch("requests.post", side_effect=post):
        assert auth(JELLYFIN_ONLY).validate_jellyfin_login("mia", "pw") == (
            None, "Only Jellyfin administrators can sign in")
    assert calls[-1][0].endswith("/Sessions/Logout")  # still logged out


@pytest.mark.parametrize("status, message", [(401, "Invalid username or password"),
                                             (500, "Jellyfin couldn't sign you in (HTTP 500)")])
def test_a_refused_sign_in_says_why(auth, status, message):
    post, calls = _jellyfin_answers(status=status)
    with patch("requests.post", side_effect=post):
        assert auth(JELLYFIN_ONLY).validate_jellyfin_login("roger", "nope") == (None, message)


def test_an_unreachable_jellyfin_is_named(auth):
    with patch("requests.post", side_effect=requests.ConnectionError("refused")):
        account, error = auth(JELLYFIN_ONLY).validate_jellyfin_login("roger", "pw")
    assert account is None and "Couldn't reach Jellyfin" in error


def test_without_jellyfin_there_is_no_jellyfin_sign_in(auth):
    service = auth(PLEX)
    with patch("requests.post") as post:
        assert service.validate_jellyfin_login("roger", "pw") == (None, "Jellyfin sign-in isn't set up")
    post.assert_not_called()
    assert service.login_methods() == {"plex": True, "jellyfin": False, "password": False}


# --- the login page and route ---------------------------------------------------------

@pytest.fixture
def login_client(auth):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web.routers import auth as auth_router

    def make(settings):
        service = auth(settings)
        app = FastAPI()
        app.include_router(auth_router.router, prefix="/auth")
        patcher = patch("web.routers.auth.get_auth_service", return_value=service)
        patcher.start()
        return TestClient(app, follow_redirects=False), service, patcher

    patchers = []

    def wrapped(settings):
        client, service, patcher = make(settings)
        patchers.append(patcher)
        return client, service

    yield wrapped
    for patcher in patchers:
        patcher.stop()


def test_the_login_page_offers_the_sign_ins_in_use(login_client):
    jellyfin_only, _ = login_client(JELLYFIN_ONLY)
    page = jellyfin_only.get("/auth/login").text
    assert 'action="/auth/login/jellyfin"' in page and 'id="plex-oauth-btn"' not in page
    assert 'id="jellyfin-btn"' not in page  # the only way in: the form shows at once

    plex_only, _ = login_client(PLEX)
    page = plex_only.get("/auth/login").text
    assert 'id="plex-oauth-btn"' in page and "/auth/login/jellyfin" not in page

    both, _ = login_client({**PLEX, **JELLYFIN})
    page = both.get("/auth/login").text
    assert 'id="plex-oauth-btn"' in page and 'id="jellyfin-btn"' in page


def test_signing_in_with_jellyfin_sets_a_session(login_client):
    client, service = login_client(JELLYFIN_ONLY)
    post, _ = _jellyfin_answers()
    with patch("requests.post", side_effect=post):
        response = client.post("/auth/login/jellyfin", data={"username": "roger", "password": "pw",
                                                             "next_url": "/settings"})
    assert response.status_code == 302 and response.headers["location"] == "/settings"
    session = service.validate_session(response.cookies["plexcache_session"])
    assert session.plex_username == "roger" and session.plex_id == "jellyfin:u-roger"


def test_a_refused_jellyfin_sign_in_keeps_its_form_open(login_client):
    client, _ = login_client({**PLEX, **JELLYFIN})
    post, _ = _jellyfin_answers(admin=False)
    with patch("requests.post", side_effect=post):
        response = client.post("/auth/login/jellyfin", data={"username": "mia", "password": "pw"})
    assert response.status_code == 401 and "Only Jellyfin administrators can sign in" in response.text
    form = re.search(r'<form method="post" action="/auth/login/jellyfin"[^>]*>', response.text).group(0)
    assert "display: none" not in form
    assert "plexcache_session" not in response.cookies


# --- Settings > Security ------------------------------------------------------------------

def _save_security(service, data):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web.routers import settings as settings_router
    settings_service = MagicMock()
    settings_service.get_security_settings.return_value = {"auth_enabled": False, "auth_session_hours": 24}
    settings_service.save_security_settings.return_value = True
    app = FastAPI()
    app.include_router(settings_router.router, prefix="/settings")
    with patch("web.routers.settings.get_settings_service", return_value=settings_service), \
            patch("web.services.auth_service.get_auth_service", return_value=service):
        response = TestClient(app).put("/settings/security", data=data)
    return response, settings_service


def test_login_can_be_switched_on_without_plex(auth):
    service = auth({**JELLYFIN_ONLY, "auth_enabled": False})
    service.capture_admin_identity = MagicMock()
    response, settings_service = _save_security(service, {"auth_enabled": "true"})
    assert "Security settings saved" in response.text
    service.capture_admin_identity.assert_not_called()
    assert settings_service.save_security_settings.call_args.args[0]["auth_enabled"] is True


def test_without_any_media_server_login_needs_a_password(auth):
    service = auth({"plex_enabled": False, "auth_enabled": False})
    response, settings_service = _save_security(service, {"auth_enabled": "true"})
    assert "Turn on Enable Password Login" in response.text
    settings_service.save_security_settings.assert_not_called()


def test_plex_installs_still_capture_the_owner(auth):
    service = auth({**PLEX, "auth_enabled": False})
    service.capture_admin_identity = MagicMock(return_value={"account_id": "1", "username": "owner"})
    response, settings_service = _save_security(service, {"auth_enabled": "true"})
    service.capture_admin_identity.assert_called_once()
    assert settings_service.save_security_settings.call_args.args[0]["auth_admin_plex_id"] == "1"
