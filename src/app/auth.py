import base64
import hashlib
import hmac
import os

from fastapi import Depends, HTTPException, Request

from .db import Database, init_schema

# Team of four: one admin, three agents. All start with DEMO_PASSWORD.
SEED_TEAM = [
    ("admin@support.test", "Alex (Admin)", "admin"),
    ("sam@support.test", "Sam", "agent"),
    ("jordan@support.test", "Jordan", "agent"),
    ("riley@support.test", "Riley", "agent"),
]


def _pbkdf2_sha256(password: bytes, salt: bytes, iterations: int) -> bytes:
    """PBKDF2-HMAC-SHA256 (32-byte key). Uses hashlib's C version when present;
    Cloudflare's Python runtime has no OpenSSL, so it falls back to this loop."""
    if hasattr(hashlib, "pbkdf2_hmac"):
        return hashlib.pbkdf2_hmac("sha256", password, salt, iterations)
    key = (hashlib.sha256(password).digest() if len(password) > 64 else password).ljust(64, b"\0")
    inner, outer = hashlib.sha256(bytes(b ^ 0x36 for b in key)), hashlib.sha256(bytes(b ^ 0x5C for b in key))

    def prf(msg: bytes) -> bytes:
        i, o = inner.copy(), outer.copy()
        i.update(msg)
        o.update(i.digest())
        return o.digest()

    u = prf(salt + b"\x00\x00\x00\x01")
    acc = int.from_bytes(u, "big")
    for _ in range(iterations - 1):
        u = prf(u)
        acc ^= int.from_bytes(u, "big")
    return acc.to_bytes(32, "big")


def hash_password(password: str, iterations: int) -> str:
    salt = os.urandom(16)
    key = _pbkdf2_sha256(password.encode(), salt, iterations)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"pbkdf2_sha256${iterations}${b64(salt)}${b64(key)}"


def check_password(stored: str, password: str) -> bool:
    try:
        algo, rounds, salt, key = stored.split("$")
        assert algo == "pbkdf2_sha256"
        got = _pbkdf2_sha256(password.encode(), base64.b64decode(salt), int(rounds))
        return hmac.compare_digest(got, base64.b64decode(key))
    except Exception:
        return False


async def seed_users(db: Database, password: str, iterations: int) -> None:
    if (await db.query("SELECT COUNT(*) AS n FROM users"))[0]["n"]:
        return
    for email, name, role in SEED_TEAM:
        await db.execute(
            "INSERT INTO users (email, name, role, password_hash) VALUES (?, ?, ?, ?) ON CONFLICT(email) DO NOTHING",
            (email, name, role, hash_password(password, iterations)),
        )


async def get_db(request: Request) -> Database:
    """Creates tables and seed users on first use (works on Workers, which has no startup hook)."""
    state = request.app.state
    if not state.ready:
        async with state.init_lock:
            if not state.ready:
                await init_schema(state.db)
                await seed_users(state.db, state.settings.demo_password, state.settings.pbkdf2_iterations)
                state.ready = True
    return state.db


async def current_user(request: Request, db: Database = Depends(get_db)) -> dict:
    """The session cookie only holds the user id; role is re-read each request."""
    user_id = request.session.get("user_id")
    rows = await db.query("SELECT id, email, name, role FROM users WHERE id = ?", (user_id,)) if user_id else []
    if not rows:
        raise HTTPException(401, "Sign in required")
    return rows[0]


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Only admins can do this")
    return user
