"""Client keys (wk_…). One per client (owner, claude-code, codex, …); only SHA-256 hashes on disk.
Any valid key also unlocks the OAuth login page and the upload page."""

from __future__ import annotations

import hashlib
import hmac
import json
import contextlib
import fcntl
import os
import threading
import secrets
from datetime import date
from pathlib import Path

from wikicore.vault import slugify


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def write_private(path: Path, text: str) -> None:
    """Atomic (tmp + replace) write of a secret-bearing file; the tmp file is created 0600 so it is never readable by others."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    tmp.chmod(0o600)
    tmp.replace(path)


_LOCKS: dict[str, tuple[threading.RLock, list]] = {}


@contextlib.contextmanager
def file_lock(path: Path):
    """Exclusive, re-entrant lock for a read-modify-write of `path`: a thread lock plus fcntl.flock on the
    sidecar `<path>.lock`, so the CLI and the server never overwrite each other's changes. POSIX only."""
    path = Path(path)
    tl, state = _LOCKS.setdefault(str(path.resolve()), (threading.RLock(), [0, None]))
    with tl:
        if state[0] == 0:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path.with_name(path.name + ".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            state[1] = fd
        state[0] += 1
        try:
            yield
        finally:
            state[0] -= 1
            if state[0] == 0:
                os.close(state[1])   # closing releases the flock
                state[1] = None


class TokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        write_private(self.path, json.dumps(data, indent=1))

    def add(self, client: str) -> str:
        key = "wk_" + secrets.token_urlsafe(32)
        with file_lock(self.path):
            data = self._load()
            data[slugify(client)] = {"hash": _hash(key), "created": date.today().isoformat()}
            self._save(data)
        return key

    def resolve(self, key: str | None) -> str | None:
        if not key or not key.startswith("wk_"):
            return None
        h = _hash(key)
        for client, rec in self._load().items():
            if isinstance(rec, dict) and hmac.compare_digest(str(rec.get("hash", "")), h):
                return client
        return None

    def revoke(self, client: str) -> bool:
        with file_lock(self.path):
            data = self._load()
            gone = data.pop(slugify(client), None) is not None
            self._save(data)
        return gone

    def hash_active(self, key_hash: str) -> bool:
        """True while a key with this hash is still registered (used to end OAuth sessions minted by a revoked key)."""
        return any(isinstance(r, dict) and hmac.compare_digest(str(r.get("hash", "")), key_hash) for r in self._load().values())

    def list(self) -> list[str]:
        return sorted(self._load())
