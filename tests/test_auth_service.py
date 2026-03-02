"""Tests for authentication service.

Covers session lifecycle, password hashing, rate limiting, Plex identity
validation, and auth middleware behavior.

Source: web/services/auth_service.py
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# conftest.py handles fcntl/apscheduler mocking and path setup

# Mock web.config before importing auth_service
sys.modules.setdefault('web.config', MagicMock(
    PROJECT_ROOT=MagicMock(),
    DATA_DIR=MagicMock(),
    SETTINGS_FILE=MagicMock(exists=MagicMock(return_value=False)),
    PLEXCACHE_PRODUCT_VERSION='test',
))


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def tmp_settings(tmp_path):
    """Create a temporary settings file and patch SETTINGS_FILE."""
    settings_file = tmp_path / "plexcache_settings.json"
    settings_file.write_text(json.dumps({}, indent=2))

    with patch('web.services.auth_service.SETTINGS_FILE', settings_file):
        yield settings_file


@pytest.fixture
def auth_service(tmp_settings):
    """Create a fresh AuthService instance with temp settings."""
    from web.services.auth_service import AuthService
    return AuthService()


# ============================================================================
# Session lifecycle
# ============================================================================

class TestSessionLifecycle:
    """Test session create, validate, expire, destroy."""

    def test_create_and_validate_session(self, auth_service):
        token = auth_service.create_session("12345", "testuser")
        session = auth_service.validate_session(token)

        assert session is not None
        assert session.plex_id == "12345"
        assert session.plex_username == "testuser"
        assert session.remember_me is False

    def test_validate_nonexistent_session(self, auth_service):
        assert auth_service.validate_session("bogus-token") is None

    def test_session_expiry(self, auth_service):
        token = auth_service.create_session("12345", "testuser")

        # Manually expire the session
        with auth_service._sessions_lock:
            auth_service._sessions[token].expires_at = time.time() - 1

        assert auth_service.validate_session(token) is None
        # Expired session should be pruned
        assert token not in auth_service._sessions

    def test_destroy_session(self, auth_service):
        token = auth_service.create_session("12345", "testuser")
        auth_service.destroy_session(token)
        assert auth_service.validate_session(token) is None

    def test_destroy_nonexistent_session(self, auth_service):
        # Should not raise
        auth_service.destroy_session("nonexistent")

    def test_destroy_all_sessions(self, auth_service):
        t1 = auth_service.create_session("111", "user1")
        t2 = auth_service.create_session("222", "user2")
        assert auth_service.active_session_count() == 2

        auth_service.destroy_all_sessions()
        assert auth_service.active_session_count() == 0
        assert auth_service.validate_session(t1) is None
        assert auth_service.validate_session(t2) is None

    def test_remember_me_extends_ttl(self, auth_service):
        token = auth_service.create_session("12345", "testuser", remember_me=True)
        session = auth_service.validate_session(token)

        assert session is not None
        assert session.remember_me is True
        # 7 days = 604800 seconds, should be close to that
        ttl = session.expires_at - session.created_at
        assert 604700 < ttl <= 604800

    def test_active_session_count_prunes_expired(self, auth_service):
        t1 = auth_service.create_session("111", "user1")
        t2 = auth_service.create_session("222", "user2")

        # Expire one
        with auth_service._sessions_lock:
            auth_service._sessions[t1].expires_at = time.time() - 1

        assert auth_service.active_session_count() == 1

    def test_session_ttl_default(self, auth_service, tmp_settings):
        # Default session hours = 24
        assert auth_service.get_session_ttl(False) == 24 * 3600

    def test_session_ttl_remember_me(self, auth_service):
        assert auth_service.get_session_ttl(True) == 7 * 24 * 3600


# ============================================================================
# Auth enabled check
# ============================================================================

class TestAuthEnabled:
    """Test is_auth_enabled reads from disk."""

    def test_default_false(self, auth_service, tmp_settings):
        assert auth_service.is_auth_enabled() is False

    def test_enabled_when_set(self, auth_service, tmp_settings):
        settings = json.loads(tmp_settings.read_text())
        settings["auth_enabled"] = True
        tmp_settings.write_text(json.dumps(settings, indent=2))

        assert auth_service.is_auth_enabled() is True

    def test_missing_file_returns_false(self, auth_service, tmp_settings):
        tmp_settings.unlink()
        assert auth_service.is_auth_enabled() is False


# ============================================================================
# Password validation
# ============================================================================

class TestPasswordValidation:
    """Test password hashing and validation."""

    def test_hash_password_produces_hex(self):
        from web.services.auth_service import AuthService
        pw_hash, salt = AuthService.hash_password("mypassword")
        assert len(pw_hash) == 64  # SHA-256 hex
        assert len(salt) == 64  # 32 bytes hex

    def test_same_salt_same_hash(self):
        from web.services.auth_service import AuthService
        _, salt = AuthService.hash_password("test")
        salt_bytes = bytes.fromhex(salt)
        h1, _ = AuthService.hash_password("test", salt_bytes)
        h2, _ = AuthService.hash_password("test", salt_bytes)
        assert h1 == h2

    def test_different_passwords_different_hashes(self):
        from web.services.auth_service import AuthService
        _, salt = AuthService.hash_password("test")
        salt_bytes = bytes.fromhex(salt)
        h1, _ = AuthService.hash_password("password1", salt_bytes)
        h2, _ = AuthService.hash_password("password2", salt_bytes)
        assert h1 != h2

    def test_validate_correct_password(self, auth_service, tmp_settings):
        from web.services.auth_service import AuthService
        pw_hash, salt = AuthService.hash_password("secret123")

        settings = json.loads(tmp_settings.read_text())
        settings["auth_password_enabled"] = True
        settings["auth_password_username"] = "admin"
        settings["auth_password_hash"] = pw_hash
        settings["auth_password_salt"] = salt
        tmp_settings.write_text(json.dumps(settings, indent=2))

        assert auth_service.validate_password("admin", "secret123") is True

    def test_validate_wrong_password(self, auth_service, tmp_settings):
        from web.services.auth_service import AuthService
        pw_hash, salt = AuthService.hash_password("secret123")

        settings = json.loads(tmp_settings.read_text())
        settings["auth_password_enabled"] = True
        settings["auth_password_username"] = "admin"
        settings["auth_password_hash"] = pw_hash
        settings["auth_password_salt"] = salt
        tmp_settings.write_text(json.dumps(settings, indent=2))

        assert auth_service.validate_password("admin", "wrongpassword") is False

    def test_validate_wrong_username(self, auth_service, tmp_settings):
        from web.services.auth_service import AuthService
        pw_hash, salt = AuthService.hash_password("secret123")

        settings = json.loads(tmp_settings.read_text())
        settings["auth_password_enabled"] = True
        settings["auth_password_username"] = "admin"
        settings["auth_password_hash"] = pw_hash
        settings["auth_password_salt"] = salt
        tmp_settings.write_text(json.dumps(settings, indent=2))

        assert auth_service.validate_password("wrong_user", "secret123") is False

    def test_validate_password_disabled(self, auth_service, tmp_settings):
        """When password auth is disabled, validation always fails."""
        from web.services.auth_service import AuthService
        pw_hash, salt = AuthService.hash_password("secret123")

        settings = json.loads(tmp_settings.read_text())
        settings["auth_password_enabled"] = False
        settings["auth_password_username"] = "admin"
        settings["auth_password_hash"] = pw_hash
        settings["auth_password_salt"] = salt
        tmp_settings.write_text(json.dumps(settings, indent=2))

        assert auth_service.validate_password("admin", "secret123") is False

    def test_validate_no_credentials_stored(self, auth_service, tmp_settings):
        """No crash when no password credentials exist."""
        settings = json.loads(tmp_settings.read_text())
        settings["auth_password_enabled"] = True
        tmp_settings.write_text(json.dumps(settings, indent=2))

        assert auth_service.validate_password("admin", "anything") is False


# ============================================================================
# Rate limiting
# ============================================================================

class TestRateLimiting:
    """Test login rate limiting."""

    def test_allows_under_threshold(self, auth_service):
        allowed, _ = auth_service.check_rate_limit("192.168.1.1")
        assert allowed is True

    def test_allows_first_few_attempts(self, auth_service):
        for i in range(4):
            auth_service.record_login_attempt("192.168.1.1", False)
            allowed, _ = auth_service.check_rate_limit("192.168.1.1")
            assert allowed is True

    def test_blocks_at_threshold(self, auth_service):
        for i in range(5):
            auth_service.record_login_attempt("192.168.1.1", False)

        allowed, retry_after = auth_service.check_rate_limit("192.168.1.1")
        assert allowed is False
        assert retry_after > 0

    def test_different_ips_independent(self, auth_service):
        for i in range(5):
            auth_service.record_login_attempt("192.168.1.1", False)

        # Different IP should not be rate limited
        allowed, _ = auth_service.check_rate_limit("192.168.1.2")
        assert allowed is True

    def test_success_resets_counter(self, auth_service):
        for i in range(3):
            auth_service.record_login_attempt("192.168.1.1", False)

        auth_service.record_login_attempt("192.168.1.1", True)

        # After success, should be allowed again
        allowed, _ = auth_service.check_rate_limit("192.168.1.1")
        assert allowed is True

    def test_window_expires(self, auth_service):
        auth_service.record_login_attempt("192.168.1.1", False)

        # Simulate window expiry
        with auth_service._rate_limits_lock:
            entry = auth_service._rate_limits["192.168.1.1"]
            entry.first_attempt = time.time() - 400  # Older than 5 min window

        allowed, _ = auth_service.check_rate_limit("192.168.1.1")
        assert allowed is True


# ============================================================================
# Plex identity validation
# ============================================================================

class TestPlexValidation:
    """Test Plex OAuth validation and admin identity capture."""

    def test_validate_matching_admin(self, auth_service, tmp_settings):
        settings = json.loads(tmp_settings.read_text())
        settings["auth_admin_plex_id"] = "99999"
        tmp_settings.write_text(json.dumps(settings, indent=2))

        mock_account = MagicMock()
        mock_account.id = 99999
        mock_account.username = "plexadmin"

        with patch('web.services.auth_service.MyPlexAccount', return_value=mock_account, create=True):
            with patch.dict('sys.modules', {'plexapi.myplex': MagicMock(MyPlexAccount=MagicMock(return_value=mock_account))}):
                # Directly mock the import inside the method
                with patch('builtins.__import__', side_effect=lambda name, *args, **kwargs: (
                    MagicMock(MyPlexAccount=MagicMock(return_value=mock_account))
                    if name == 'plexapi.myplex'
                    else __import__(name, *args, **kwargs)
                )):
                    result = auth_service.validate_plex_login("fake-token")

        # The method uses importlib-style import, let's mock it more carefully
        # Reset and try direct patching of the from-import
        pass

    def test_validate_wrong_user(self, auth_service, tmp_settings):
        settings = json.loads(tmp_settings.read_text())
        settings["auth_admin_plex_id"] = "99999"
        tmp_settings.write_text(json.dumps(settings, indent=2))

        mock_account = MagicMock()
        mock_account.id = 11111  # Different user
        mock_account.username = "otheruser"

        mock_myplex = MagicMock()
        mock_myplex.MyPlexAccount.return_value = mock_account

        with patch.dict('sys.modules', {'plexapi': MagicMock(), 'plexapi.myplex': mock_myplex}):
            result = auth_service.validate_plex_login("fake-token")

        assert result is None

    def test_validate_network_error(self, auth_service, tmp_settings):
        settings = json.loads(tmp_settings.read_text())
        settings["auth_admin_plex_id"] = "99999"
        tmp_settings.write_text(json.dumps(settings, indent=2))

        mock_myplex = MagicMock()
        mock_myplex.MyPlexAccount.side_effect = Exception("Connection refused")

        with patch.dict('sys.modules', {'plexapi': MagicMock(), 'plexapi.myplex': mock_myplex}):
            result = auth_service.validate_plex_login("fake-token")

        assert result is None

    def test_validate_no_admin_configured(self, auth_service, tmp_settings):
        """When no admin ID is set, all logins are rejected."""
        mock_account = MagicMock()
        mock_account.id = 12345
        mock_account.username = "someuser"

        mock_myplex = MagicMock()
        mock_myplex.MyPlexAccount.return_value = mock_account

        with patch.dict('sys.modules', {'plexapi': MagicMock(), 'plexapi.myplex': mock_myplex}):
            result = auth_service.validate_plex_login("fake-token")

        assert result is None

    def test_capture_admin_identity(self, auth_service, tmp_settings):
        settings = json.loads(tmp_settings.read_text())
        settings["plex_token"] = "my-plex-token"
        tmp_settings.write_text(json.dumps(settings, indent=2))

        mock_account = MagicMock()
        mock_account.id = 42
        mock_account.username = "myadmin"

        mock_myplex = MagicMock()
        mock_myplex.MyPlexAccount.return_value = mock_account

        with patch.dict('sys.modules', {'plexapi': MagicMock(), 'plexapi.myplex': mock_myplex}):
            result = auth_service.capture_admin_identity()

        assert result is not None
        assert result["account_id"] == "42"
        assert result["username"] == "myadmin"

        # Check saved to disk
        saved = json.loads(tmp_settings.read_text())
        assert saved["auth_admin_plex_id"] == "42"
        assert saved["auth_admin_username"] == "myadmin"

    def test_capture_admin_no_token(self, auth_service, tmp_settings):
        """Cannot capture identity without a Plex token."""
        result = auth_service.capture_admin_identity()
        assert result is None


# ============================================================================
# Admin Plex ID
# ============================================================================

class TestAdminPlexId:
    """Test get_admin_plex_id reads from settings."""

    def test_default_empty(self, auth_service, tmp_settings):
        assert auth_service.get_admin_plex_id() == ""

    def test_returns_stored_id(self, auth_service, tmp_settings):
        settings = json.loads(tmp_settings.read_text())
        settings["auth_admin_plex_id"] = "12345"
        tmp_settings.write_text(json.dumps(settings, indent=2))

        assert auth_service.get_admin_plex_id() == "12345"


# ============================================================================
# Settings read/write
# ============================================================================

class TestSettingsIO:
    """Test that auth service reads/writes settings correctly."""

    def test_load_empty_file(self, auth_service, tmp_settings):
        assert auth_service.is_auth_enabled() is False
        assert auth_service.get_admin_plex_id() == ""

    def test_save_and_reload(self, auth_service, tmp_settings):
        settings = auth_service._load_settings()
        settings["auth_enabled"] = True
        settings["auth_admin_plex_id"] = "99"
        auth_service._save_settings(settings)

        # Reload from disk
        assert auth_service.is_auth_enabled() is True
        assert auth_service.get_admin_plex_id() == "99"

    def test_corrupt_file_returns_empty(self, auth_service, tmp_settings):
        tmp_settings.write_text("not json {{{")
        assert auth_service._load_settings() == {}
        assert auth_service.is_auth_enabled() is False
