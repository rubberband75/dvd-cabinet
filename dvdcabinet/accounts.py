"""User accounts, passwords and login sessions (no web code here; see web_auth.py)."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from .db import Database

SESSION_DAYS = 30
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MIN_PASSWORD = 8
# scrypt with 16 MiB of memory per hash: slow enough to resist guessing, ~50 ms to check
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


class AccountError(ValueError):
    """Something the user can fix (shown to them as-is)."""


@dataclass(frozen=True)
class User:
    id: int
    username: str
    is_admin: bool
    created_at: int
    last_login_at: int | None

    def to_json(self) -> dict:
        return {"id": self.id, "username": self.username, "is_admin": self.is_admin,
                "created_at": self.created_at, "last_login_at": self.last_login_at}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, maxmem=64 * 1024 * 1024, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        check = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                               dklen=len(digest) // 2, maxmem=64 * 1024 * 1024)
    except ValueError:
        return False
    return hmac.compare_digest(check.hex(), digest)


# Checked when a username doesn't exist, so a wrong username takes as long as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _row_user(row) -> User:
    return User(row["id"], row["username"], bool(row["is_admin"]), row["created_at"], row["last_login_at"])


class LoginThrottle:
    """At most `limit` failed logins per key (address, username) in `window` seconds."""

    def __init__(self, limit: int = 10, window: float = 15 * 60):
        self.limit = limit
        self.window = window
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, *keys: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return any(len(self._recent(k, now)) >= self.limit for k in keys)

    def failed(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for k in keys:
                self._recent(k, now).append(now)

    def succeeded(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._failures.pop(k, None)


class Accounts:
    def __init__(self, db: Database):
        self.db = db
        self.throttle = LoginThrottle()
        # Needed to claim the server from outside the local network; printed in the log.
        self.setup_code = "-".join(secrets.token_hex(2).upper() for _ in range(3))

    # ---- users ------------------------------------------------------------------

    def needs_setup(self) -> bool:
        return self.db.one("SELECT 1 FROM users LIMIT 1") is None

    def list_users(self) -> list[User]:
        return [_row_user(r) for r in self.db.all("SELECT * FROM users ORDER BY username COLLATE NOCASE")]

    def get_user(self, user_id: int) -> User | None:
        row = self.db.one("SELECT * FROM users WHERE id = ?", (user_id,))
        return _row_user(row) if row else None

    def create_user(self, username: str, password: str, is_admin: bool = False, only_if_first: bool = False) -> User:
        username = username.strip()
        if not USERNAME_RE.match(username):
            raise AccountError("Usernames are 1–64 letters, numbers, dots, dashes or underscores.")
        self._check_password(password)
        # only_if_first: a single statement, so two people claiming at once can't both win
        guard = " WHERE NOT EXISTS (SELECT 1 FROM users)" if only_if_first else ""
        try:
            cur = self.db.execute(
                f"INSERT INTO users (username, password_hash, is_admin, created_at) SELECT ?, ?, ?, ?{guard}",
                (username, hash_password(password), int(is_admin), int(time.time())))
        except sqlite3.IntegrityError:
            raise AccountError(f"There's already a user called {username}.") from None
        if cur.rowcount == 0:
            raise AccountError("This server has already been claimed.")
        return self.get_user(cur.lastrowid)

    def claim(self, username: str, password: str) -> User:
        """Create the first (admin) account; only possible while there are no users."""
        user = self.create_user(username, password, is_admin=True, only_if_first=True)
        self.db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (int(time.time()), user.id))
        return self.get_user(user.id)

    def set_password(self, user_id: int, password: str) -> None:
        self._check_password(password)
        self.db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
        self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))  # sign out everywhere

    def set_admin(self, user_id: int, is_admin: bool) -> None:
        if not is_admin and self._is_last_admin(user_id):
            raise AccountError("You can't remove the last admin.")
        self.db.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))

    def delete_user(self, user_id: int) -> None:
        if self._is_last_admin(user_id):
            raise AccountError("You can't delete the last admin.")
        self.db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def _is_last_admin(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if user is None or not user.is_admin:
            return False
        return self.db.one("SELECT COUNT(*) AS n FROM users WHERE is_admin = 1")["n"] <= 1

    @staticmethod
    def _check_password(password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise AccountError(f"Passwords need at least {MIN_PASSWORD} characters.")

    def authenticate(self, username: str, password: str) -> User | None:
        row = self.db.one("SELECT * FROM users WHERE username = ?", (username.strip(),))
        if row is None:
            verify_password(password, _DUMMY_HASH)
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        self.db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (int(time.time()), row["id"]))
        return _row_user(row)

    # ---- sessions -----------------------------------------------------------------

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        self.db.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (_token_hash(token), user_id, now, now, now + SESSION_DAYS * 86400))
        return token

    def session_user(self, token: str) -> User | None:
        """The user a session cookie belongs to; sessions are extended while in use."""
        now = int(time.time())
        row = self.db.one(
            "SELECT u.*, s.last_seen_at AS seen FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?", (_token_hash(token), now))
        if row is None:
            return None
        if now - row["seen"] > 3600:  # touch at most hourly
            self.db.execute("UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
                            (now, now + SESSION_DAYS * 86400, _token_hash(token)))
        return _row_user(row)

    def end_session(self, token: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def prune_sessions(self) -> None:
        self.db.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
