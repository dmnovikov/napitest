"""Операторы, пароли (PBKDF2) и сессии."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from ..db.database import Database, now_iso, parse_iso, to_iso

_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
    return hmac.compare_digest(digest.hex(), digest_hex)


class AuthService:
    def __init__(self, db: Database, session_hours: float):
        self.db = db
        self.session_hours = session_hours

    def ensure_operator(self, username: str, password: str) -> None:
        """Создаёт оператора при первом запуске; существующий пароль не перезаписывает."""
        if not password:
            return
        if self.db.one("SELECT id FROM operator WHERE username = ?", (username,)):
            return
        self.db.execute(
            "INSERT INTO operator (username, pw_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), now_iso()),
        )

    def set_password(self, username: str, password: str) -> None:
        pw_hash = hash_password(password)
        if self.db.one("SELECT id FROM operator WHERE username = ?", (username,)):
            self.db.execute("UPDATE operator SET pw_hash = ? WHERE username = ?", (pw_hash, username))
        else:
            self.db.execute(
                "INSERT INTO operator (username, pw_hash, created_at) VALUES (?, ?, ?)",
                (username, pw_hash, now_iso()),
            )

    def login(self, username: str, password: str) -> str | None:
        row = self.db.one("SELECT id, pw_hash FROM operator WHERE username = ?", (username,))
        if not row or not verify_password(password, row["pw_hash"]):
            return None
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(hours=self.session_hours)
        self.db.execute(
            "INSERT INTO session (token, operator_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, row["id"], now_iso(), to_iso(expires)),
        )
        return token

    def check_password(self, operator_id: int, password: str) -> bool:
        """Повторный ввод пароля перед ответственным действием (смена образа ОС, Custom)."""
        row = self.db.one("SELECT pw_hash FROM operator WHERE id = ?", (operator_id,))
        return bool(row) and verify_password(password or "", row["pw_hash"])

    def logout(self, token: str) -> None:
        self.db.execute("DELETE FROM session WHERE token = ?", (token,))

    def operator_for(self, token: str | None) -> dict | None:
        if not token:
            return None
        row = self.db.one(
            "SELECT o.id, o.username, s.expires_at FROM session s JOIN operator o ON o.id = s.operator_id"
            " WHERE s.token = ?",
            (token,),
        )
        if not row:
            return None
        if parse_iso(row["expires_at"]) < datetime.now(timezone.utc):
            self.logout(token)
            return None
        return {"id": row["id"], "username": row["username"]}
