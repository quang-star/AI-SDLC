"""Application runtime state, dependency wiring, and in-memory rate limiting."""

from __future__ import annotations

import socket
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ai_sdlc_saas.config import Settings, get_settings
from ai_sdlc_saas.db.engine import build_engine, create_session_factory, init_db_schema
from ai_sdlc_saas.errors import RateLimitError
from ai_sdlc_saas.security.crypto import EnvelopeCipher
from ai_sdlc_saas.storage.object_store import LocalObjectStore


def _encode_resp(*parts: str) -> bytes:
    chunks = [f"*{len(parts)}\r\n".encode("ascii")]
    for part in parts:
        raw = part.encode("utf-8")
        chunks.append(f"${len(raw)}\r\n".encode("ascii"))
        chunks.append(raw + b"\r\n")
    return b"".join(chunks)


def _redis_incr_window(redis_url: str, key: str, window_seconds: int) -> int | None:
    """Increment fixed-window counter in Redis via RESP over TCP; return None on connection error."""
    parsed = urlparse(redis_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 6379
    password = unquote(parsed.password) if parsed.password else ""
    db_raw = parsed.path.lstrip("/")
    db_index = int(db_raw) if db_raw.isdigit() else 0
    window_bucket = int(time.time() // window_seconds)
    redis_key = f"ai_sdlc:rl:{key}:{window_bucket}"

    try:
        with socket.create_connection((host, port), timeout=1.5) as sock:
            sock.settimeout(1.5)
            commands: list[bytes] = []
            if password:
                commands.append(_encode_resp("AUTH", password))
            if db_index > 0:
                commands.append(_encode_resp("SELECT", str(db_index)))
            commands.append(_encode_resp("INCR", redis_key))
            sock.sendall(b"".join(commands))
            reader = sock.makefile("rb")
            replies: list[str] = []
            for _ in commands:
                line = reader.readline().decode("utf-8", errors="replace").strip()
                if not line or line.startswith("-"):
                    return None
                replies.append(line)
            incr_reply = replies[-1]
            if not incr_reply.startswith(":"):
                return None
            count = int(incr_reply[1:])
            if count == 1:
                sock.sendall(_encode_resp("EXPIRE", redis_key, str(max(2, window_seconds * 2))))
                _ = reader.readline()
            return count
    except (OSError, ValueError):
        return None


class SlidingWindowRateLimiter:
    """Thread-safe per-key request rate limiter with optional Redis backing."""

    def __init__(self, max_requests: int = 120, window_seconds: int = 60, *, redis_url: str = "") -> None:
        self.max_requests = max(1, max_requests)
        self.window_seconds = max(1, window_seconds)
        self.redis_url = redis_url.strip()
        self._lock = threading.Lock()
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, *, limit_override: int | None = None) -> None:
        limit = max(1, limit_override or self.max_requests)
        if self.redis_url:
            count = _redis_incr_window(self.redis_url, key, self.window_seconds)
            if count is not None:
                if count > limit:
                    raise RateLimitError(
                        "Rate limit exceeded; please retry shortly.",
                        details={"window_seconds": self.window_seconds, "limit": limit, "backend": "redis"},
                    )
                return
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._buckets[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                raise RateLimitError(
                    "Rate limit exceeded; please retry shortly.",
                    details={"window_seconds": self.window_seconds, "limit": limit},
                )
            bucket.append(now)


@dataclass
class AppRuntime:
    """Shared dependencies held on `app.state.runtime`."""

    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    object_store: LocalObjectStore
    cipher: EnvelopeCipher
    rate_limiter: SlidingWindowRateLimiter = field(default_factory=SlidingWindowRateLimiter)
    extra: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Settings | None = None, *, init_schema: bool = True) -> AppRuntime:
        resolved = settings or get_settings()
        resolved.validate_security_posture()
        engine = build_engine(resolved.database_url)
        if init_schema:
            init_db_schema(engine)
        factory = create_session_factory(engine)
        store = LocalObjectStore(Path(resolved.object_store_dir))
        cipher = EnvelopeCipher(resolved.resolved_fernet_keys())
        limiter = SlidingWindowRateLimiter(
            max_requests=resolved.rate_limit_rpm,
            window_seconds=60,
            redis_url=resolved.redis_url,
        )
        Path(resolved.worktree_root).mkdir(parents=True, exist_ok=True)
        return cls(
            settings=resolved,
            engine=engine,
            session_factory=factory,
            object_store=store,
            cipher=cipher,
            rate_limiter=limiter,
        )
