"""Cryptographic primitives: password hashing, JWT sessions, envelope encryption, HMAC verification."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken, MultiFernet


def hash_password(password: str) -> str:
    """Hash a user password with bcrypt."""
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    salt = bcrypt.gensalt(rounds=10)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time verification of a plain password against bcrypt hash."""
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


def generate_opaque_token(prefix: str = "tok", nbytes: int = 24) -> str:
    """Generate a high-entropy URL-safe token."""
    return f"{prefix}_{secrets.token_urlsafe(nbytes)}"


def hash_token(token: str) -> str:
    """Return SHA-256 hex digest of an opaque token or session identifier."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mask_secret(secret: str) -> str:
    """Return a masked display string preserving only last 4 characters if long enough."""
    cleaned = (secret or "").strip()
    if len(cleaned) <= 8:
        return "****"
    return f"{cleaned[:3]}...****{cleaned[-4:]}"


class EnvelopeCipher:
    """Fernet/MultiFernet symmetric encryption supporting key rotation."""

    def __init__(self, fernet_keys: Sequence[str]) -> None:
        if not fernet_keys:
            raise ValueError("At least one Fernet key is required.")
        self.key_version: int = len(fernet_keys)
        self._fernet = MultiFernet([Fernet(k.encode("ascii")) for k in fernet_keys])

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            raise ValueError("Cannot encrypt empty secret.")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Secret ciphertext could not be decrypted (revoked or invalid key).") from exc

    def rotate(self, ciphertext: str) -> str:
        """Re-encrypt `ciphertext` under the primary (first) key."""
        try:
            return self._fernet.rotate(ciphertext.encode("ascii")).decode("ascii")
        except InvalidToken as exc:
            raise ValueError("Cannot rotate invalid ciphertext.") from exc


def issue_session_jwt(
    *,
    user_id: str,
    session_id: str,
    jti: str,
    jwt_secret: str,
    ttl_minutes: int = 720,
) -> tuple[str, datetime]:
    """Issue a signed HS256 JWT bound to a DB-persisted session ID and JTI."""
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=ttl_minutes)
    payload: dict[str, Any] = {
        "sub": user_id,
        "sid": session_id,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, jwt_secret, algorithm="HS256")
    return token, exp


def decode_session_jwt(token: str, *, jwt_secret: str) -> dict[str, Any]:
    """Decode and verify a session JWT; raises `ValueError` on invalid/expired token."""
    try:
        payload = jwt.decode(token, jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise ValueError(f"Invalid or expired session token: {exc}") from exc
    if not isinstance(payload, dict) or not payload.get("sub") or not payload.get("sid") or not payload.get("jti"):
        raise ValueError("Malformed session token claims.")
    return payload


def verify_github_hmac_sha256(payload_bytes: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify GitHub `X-Hub-Signature-256` header (`sha256=<hex>`)."""
    if not signature_header or not signature_header.startswith("sha256=") or not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


def compute_github_hmac_sha256(payload_bytes: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


def verify_stripe_signature(payload_bytes: bytes, signature_header: str | None, secret: str, *, tolerance_sec: int = 300) -> bool:
    """Verify Stripe-style `t=<ts>,v1=<hex>` HMAC signature."""
    if not signature_header or not secret:
        return False
    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        if "=" not in item:
            continue
        k, v = item.strip().split("=", 1)
        parts.setdefault(k, []).append(v)
    timestamps = parts.get("t") or []
    v1_sigs = parts.get("v1") or []
    if not timestamps or not v1_sigs:
        return False
    try:
        ts = int(timestamps[0])
    except ValueError:
        return False
    now_ts = int(datetime.now(UTC).timestamp())
    if abs(now_ts - ts) > tolerance_sec:
        return False
    signed_payload = f"{ts}.".encode("utf-8") + payload_bytes
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in v1_sigs)


def compute_stripe_signature(payload_bytes: bytes, secret: str, *, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(datetime.now(UTC).timestamp())
    signed_payload = f"{ts}.".encode("utf-8") + payload_bytes
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"
