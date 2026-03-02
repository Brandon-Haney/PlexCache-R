"""Authentication service for PlexCache-D Web UI.

Provides optional Plex OAuth-based login and password fallback.
Disabled by default — users enable via Settings > Security.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from web.config import SETTINGS_FILE

logger = logging.getLogger(__name__)

# Singleton
_auth_service: Optional["AuthService"] = None
_auth_service_lock = threading.Lock()


@dataclass
class Session:
    """Active user session"""
    token: str
    plex_id: str
    plex_username: str
    created_at: float
    expires_at: float
    remember_me: bool = False


@dataclass
class RateLimitEntry:
    """Rate limit state for a client IP"""
    attempts: int = 0
    first_attempt: float = 0.0
    locked_until: float = 0.0


class AuthService:
    """Manages authentication sessions, Plex identity validation, and password auth."""

    # Rate limiting constants
    RATE_LIMIT_MAX_ATTEMPTS = 5
    RATE_LIMIT_WINDOW_SECONDS = 300  # 5 minutes

    # Password hashing constants
    PBKDF2_ITERATIONS = 600_000
    SALT_LENGTH = 32

    def __init__(self):
        self._sessions: Dict[str, Session] = {}
        self._sessions_lock = threading.Lock()
        self._rate_limits: Dict[str, RateLimitEntry] = {}
        self._rate_limits_lock = threading.Lock()

    # -------------------------------------------------------------------------
    # Settings helpers (read from disk each time for immediate recovery)
    # -------------------------------------------------------------------------

    def _load_settings(self) -> Dict[str, Any]:
        """Load settings from disk. Returns empty dict on error."""
        try:
            with open(str(SETTINGS_FILE), "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, IOError):
            return {}

    def _save_settings(self, settings: Dict[str, Any]) -> bool:
        """Save settings to disk."""
        try:
            with open(str(SETTINGS_FILE), "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
            return True
        except IOError:
            return False

    def is_auth_enabled(self) -> bool:
        """Check if authentication is enabled. Reads from disk each time."""
        return bool(self._load_settings().get("auth_enabled", False))

    def get_admin_plex_id(self) -> str:
        """Get the stored admin Plex account ID."""
        return str(self._load_settings().get("auth_admin_plex_id", ""))

    def get_session_hours(self) -> int:
        """Get configured session duration in hours."""
        return int(self._load_settings().get("auth_session_hours", 24))

    # -------------------------------------------------------------------------
    # Session management
    # -------------------------------------------------------------------------

    def create_session(self, plex_id: str, username: str, remember_me: bool = False) -> str:
        """Create a new session. Returns the session token."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        session_hours = self.get_session_hours()
        ttl = (7 * 24 * 3600) if remember_me else (session_hours * 3600)

        session = Session(
            token=token,
            plex_id=plex_id,
            plex_username=username,
            created_at=now,
            expires_at=now + ttl,
            remember_me=remember_me,
        )

        with self._sessions_lock:
            self._sessions[token] = session

        logger.info("Session created for user %s", username)
        return token

    def validate_session(self, token: str) -> Optional[Session]:
        """Validate a session token. Returns Session if valid, None otherwise."""
        with self._sessions_lock:
            session = self._sessions.get(token)
            if session is None:
                return None

            if time.time() > session.expires_at:
                del self._sessions[token]
                return None

            return session

    def destroy_session(self, token: str) -> None:
        """Remove a single session."""
        with self._sessions_lock:
            self._sessions.pop(token, None)

    def destroy_all_sessions(self) -> None:
        """Remove all sessions (used when disabling auth)."""
        with self._sessions_lock:
            self._sessions.clear()
        logger.info("All sessions destroyed")

    def active_session_count(self) -> int:
        """Count non-expired sessions."""
        now = time.time()
        with self._sessions_lock:
            # Prune expired while counting
            expired = [t for t, s in self._sessions.items() if now > s.expires_at]
            for t in expired:
                del self._sessions[t]
            return len(self._sessions)

    def get_session_ttl(self, remember_me: bool = False) -> int:
        """Get session TTL in seconds."""
        if remember_me:
            return 7 * 24 * 3600  # 7 days
        return self.get_session_hours() * 3600

    # -------------------------------------------------------------------------
    # Plex identity validation
    # -------------------------------------------------------------------------

    def validate_plex_login(self, oauth_token: str) -> Optional[Dict[str, str]]:
        """Validate a Plex OAuth token against the configured server's admin.

        Connects to the Plex server using the OAuth token to get the user's
        account info, then compares against stored admin Plex ID.

        Returns:
            {"account_id": str, "username": str} on match, None on mismatch/error.
        """
        try:
            from plexapi.myplex import MyPlexAccount
            account = MyPlexAccount(token=oauth_token)
            account_id = str(account.id) if hasattr(account, 'id') else ""
            username = account.username if hasattr(account, 'username') else ""

            admin_plex_id = self.get_admin_plex_id()

            if not admin_plex_id:
                logger.warning("No admin Plex ID configured — cannot validate login")
                return None

            if account_id == admin_plex_id:
                logger.info("Plex login validated for admin user: %s", username)
                return {"account_id": account_id, "username": username}

            logger.warning(
                "Plex login rejected: account %s (%s) is not admin (%s)",
                account_id, username, admin_plex_id,
            )
            return None
        except Exception as e:
            logger.error("Plex login validation failed: %s", e)
            return None

    def capture_admin_identity(self) -> Optional[Dict[str, str]]:
        """Capture the admin's Plex identity using the configured server token.

        Called when auth is first enabled. Uses PLEX_TOKEN from settings
        to connect and record the admin's account ID/username.

        Returns:
            {"account_id": str, "username": str} on success, None on error.
        """
        settings = self._load_settings()
        plex_token = settings.get("PLEX_TOKEN", "") or settings.get("plex_token", "")

        if not plex_token:
            logger.error("Cannot capture admin identity: no PLEX_TOKEN configured")
            return None

        try:
            from plexapi.myplex import MyPlexAccount
            account = MyPlexAccount(token=plex_token)
            account_id = str(account.id) if hasattr(account, 'id') else ""
            username = account.username if hasattr(account, 'username') else ""

            if not account_id:
                logger.error("Could not determine Plex account ID")
                return None

            # Save to settings
            settings["auth_admin_plex_id"] = account_id
            settings["auth_admin_username"] = username
            self._save_settings(settings)

            logger.info("Admin identity captured: %s (ID: %s)", username, account_id)
            return {"account_id": account_id, "username": username}
        except Exception as e:
            logger.error("Failed to capture admin identity: %s", e)
            return None

    # -------------------------------------------------------------------------
    # Password authentication
    # -------------------------------------------------------------------------

    @staticmethod
    def hash_password(password: str, salt: Optional[bytes] = None) -> Tuple[str, str]:
        """Hash a password with PBKDF2-SHA256.

        Returns:
            (hash_hex, salt_hex) tuple.
        """
        if salt is None:
            salt = os.urandom(AuthService.SALT_LENGTH)
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            AuthService.PBKDF2_ITERATIONS,
        )
        return dk.hex(), salt.hex()

    def validate_password(self, username: str, password: str) -> bool:
        """Validate username/password against stored credentials.

        Uses timing-safe comparison to prevent timing attacks.
        """
        settings = self._load_settings()

        if not settings.get("auth_password_enabled", False):
            return False

        stored_username = settings.get("auth_password_username", "")
        stored_hash = settings.get("auth_password_hash", "")
        stored_salt = settings.get("auth_password_salt", "")

        if not stored_username or not stored_hash or not stored_salt:
            return False

        # Timing-safe username comparison
        username_match = hmac.compare_digest(username, stored_username)

        # Hash the provided password with stored salt
        try:
            salt_bytes = bytes.fromhex(stored_salt)
        except ValueError:
            return False

        computed_hash, _ = self.hash_password(password, salt_bytes)

        # Timing-safe hash comparison
        hash_match = hmac.compare_digest(computed_hash, stored_hash)

        return username_match and hash_match

    # -------------------------------------------------------------------------
    # Rate limiting
    # -------------------------------------------------------------------------

    def check_rate_limit(self, client_ip: str) -> Tuple[bool, int]:
        """Check if a client IP is rate limited.

        Returns:
            (allowed, retry_after_seconds). allowed=True means the request can proceed.
        """
        now = time.time()

        with self._rate_limits_lock:
            entry = self._rate_limits.get(client_ip)
            if entry is None:
                return (True, 0)

            # Check if locked out
            if entry.locked_until > now:
                return (False, int(entry.locked_until - now) + 1)

            # Check if window has expired (reset)
            if now - entry.first_attempt > self.RATE_LIMIT_WINDOW_SECONDS:
                del self._rate_limits[client_ip]
                return (True, 0)

            # Still within window — check attempt count
            if entry.attempts >= self.RATE_LIMIT_MAX_ATTEMPTS:
                entry.locked_until = entry.first_attempt + self.RATE_LIMIT_WINDOW_SECONDS
                return (False, int(entry.locked_until - now) + 1)

            return (True, 0)

    def record_login_attempt(self, client_ip: str, success: bool) -> None:
        """Record a login attempt for rate limiting."""
        now = time.time()

        with self._rate_limits_lock:
            if success:
                # Reset on successful login
                self._rate_limits.pop(client_ip, None)
                return

            entry = self._rate_limits.get(client_ip)
            if entry is None or (now - entry.first_attempt > self.RATE_LIMIT_WINDOW_SECONDS):
                # Start new window
                self._rate_limits[client_ip] = RateLimitEntry(
                    attempts=1,
                    first_attempt=now,
                )
            else:
                entry.attempts += 1


def get_auth_service() -> AuthService:
    """Get or create the singleton AuthService instance."""
    global _auth_service
    if _auth_service is None:
        with _auth_service_lock:
            if _auth_service is None:
                _auth_service = AuthService()
    return _auth_service
