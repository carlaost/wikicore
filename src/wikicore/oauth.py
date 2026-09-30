"""A minimal single-user OAuth 2.1 server so claude.ai and ChatGPT (which cannot send a bearer
header) can connect. Login = paste any client key once. Tokens are labelled with the registering
client's name (e.g. "chatgpt"), which becomes the commit author. wk_ keys are accepted directly
as bearer tokens for CLI agents."""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken, RegistrationError, TokenError, construct_redirect_uri
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from wikicore.auth import TokenStore, _hash, file_lock, write_private
from wikicore.vault import slugify

ACCESS_TTL = 24 * 3600
REFRESH_TTL = 400 * 24 * 3600
CODE_TTL = 600
PENDING_TTL = 900
UNUSED_CLIENT_TTL = 3600
MAX_CLIENTS = 200
MAX_PENDING = 100
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _redirect_ok(uri: str) -> bool:
    u = urlparse(uri)
    if u.fragment or not u.hostname or u.username is not None or u.password is not None or "@" in u.netloc:
        return False
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in LOCAL_HOSTS)


class _Txn:
    def __init__(self, p: "OAuthProvider"):
        self.p, self.cm = p, file_lock(p.state_file)

    def __enter__(self):
        self.cm.__enter__()
        if self.p._depth == 0:   # only the outermost section loads; an inner one must not undo unsaved edits
            self.p._sync()
        self.p._depth += 1
        return self.p

    def __exit__(self, *exc):
        self.p._depth -= 1
        self.cm.__exit__(None, None, None)   # release only; the exception (possibly frozen) propagates untouched
        return False


class OAuthProvider:
    """State lives in oauth.json and is re-read whenever the file changes, so a revoke from another
    process (the CLI) takes effect at once and a later save cannot resurrect revoked tokens. Access and
    refresh tokens are stored as SHA-256 hashes. Each records the wk_ key that minted it and stops
    working as soon as that key is revoked (checked at resolve time)."""

    def __init__(self, tokens: TokenStore, state_file: Path, public_url: str):
        self.tokens = tokens
        self.state_file = Path(state_file)
        self.public_url = public_url.rstrip("/")
        self.lock = threading.RLock()
        self._depth = 0
        self.pending: dict[str, dict] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.code_keys: dict[str, tuple[str, str]] = {}
        self.state: dict[str, dict] = {"clients": {}, "client_meta": {}, "access": {}, "refresh": {}}
        self._sig: tuple[int, int] | None = None
        self._sync(force=True)

    def _txn(self):
        """Exclusive read-modify-write section: cross-process lock, one fresh load, mutate, then _save().
        Nothing inside may call _sync() again; the lock guarantees no one else writes meanwhile."""
        return _Txn(self)

    def _stat(self) -> tuple[int, int] | None:
        try:
            st = self.state_file.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _sync(self, force: bool = False) -> None:
        with self.lock:
            sig = self._stat()
            if not force and sig == self._sig:
                return
            fresh: dict[str, dict] = {"clients": {}, "client_meta": {}, "access": {}, "refresh": {}}
            if sig is not None:
                try:
                    loaded = json.loads(self.state_file.read_text(encoding="utf-8") or "{}")
                    if isinstance(loaded, dict):
                        fresh |= {k: v for k, v in loaded.items() if k in fresh and isinstance(v, dict)}
                except (json.JSONDecodeError, OSError):
                    pass
            self.state, self._sig = fresh, sig
            self._prune_clients()

    def _prune_clients(self) -> None:
        cutoff = time.time() - UNUSED_CLIENT_TTL
        busy = {p["client_id"] for p in self.pending.values()}
        for cid in list(self.state["clients"]):
            meta = self.state["client_meta"].get(cid) or {}
            if not meta.get("used") and meta.get("created", 0) < cutoff and cid not in busy:
                self.state["clients"].pop(cid, None)
                self.state["client_meta"].pop(cid, None)

    def _save(self) -> None:
        with self.lock:
            now = time.time()
            for kind in ("access", "refresh"):
                self.state[kind] = {k: v for k, v in self.state[kind].items() if v["expires_at"] > now}
            write_private(self.state_file, json.dumps(self.state, indent=1))
            self._sig = self._stat()

    def _label(self, client_id: str) -> str:
        raw = self.state["clients"].get(client_id) or {}
        return slugify(raw.get("client_name") or client_id)

    # clients
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        self._sync()
        raw = self.state["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not _redirect_ok(str(uri)):
                raise RegistrationError("invalid_redirect_uri", "redirect URIs must be https (or http on localhost) and have no fragment")
        with self._txn():
            self._prune_clients()
            if len(self.state["clients"]) >= MAX_CLIENTS:
                raise RegistrationError("invalid_client_metadata", "too many registered clients; try again later")
            self.state["clients"][client_info.client_id] = client_info.model_dump(mode="json", exclude_none=True)
            self.state["client_meta"][client_info.client_id] = {"created": time.time(), "used": False}
            self._save()

    # authorize → /login → code
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._expire()
        if len(self.pending) >= MAX_PENDING:
            raise AuthorizeError("temporarily_unavailable", "too many login requests in progress; try again shortly")
        rid = secrets.token_urlsafe(24)
        self.pending[rid] = {"client_id": client.client_id, "params": params, "created": time.time()}
        return f"{self.public_url}/login?{urlencode({'req': rid})}"

    def pending_redirect_host(self, rid: str) -> str | None:
        p = self.pending.get(rid)
        if not p:
            return None
        u = urlparse(str(p["params"].redirect_uri))
        host = f"[{u.hostname}]" if u.hostname and ":" in u.hostname else (u.hostname or "")
        return f"{host}:{u.port}" if u.port else host

    def pending_client(self, rid: str) -> str | None:
        self._sync()
        self._expire()
        p = self.pending.get(rid)
        if not p:
            return None
        raw = self.state["clients"].get(p["client_id"]) or {}
        return raw.get("client_name") or p["client_id"]

    def complete_login(self, rid: str, key: str) -> str:
        self._expire()
        p = self.pending.get(rid)
        if not p:
            raise AuthorizeError("invalid_request", "this login link has expired; start again from your AI client")
        label = self.tokens.resolve(key)
        if not label:
            raise AuthorizeError("access_denied", "that key is not valid")
        params: AuthorizationParams = p["params"]
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + CODE_TTL, client_id=p["client_id"],
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource,
            subject=self._label(p["client_id"]))
        self.code_keys[code] = (label, _hash(key))
        del self.pending[rid]
        with self._txn():
            if p["client_id"] in self.state["client_meta"]:
                self.state["client_meta"][p["client_id"]]["used"] = True
                self._save()
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def _expire(self) -> None:
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if now - v["created"] < PENDING_TTL}
        self.codes = {k: v for k, v in self.codes.items() if v.expires_at > now}
        self.code_keys = {k: v for k, v in self.code_keys.items() if k in self.codes}

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        c = self.codes.get(authorization_code)
        return c if c and c.client_id == client.client_id and c.expires_at > time.time() else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        if self.codes.pop(authorization_code.code, None) is None:   # single use, even under a race
            raise TokenError("invalid_grant", "authorization code already used or expired")
        key = self.code_keys.pop(authorization_code.code, None)
        if key is None:
            raise TokenError("invalid_grant", "authorization code already used or expired")
        return self._issue(client.client_id, authorization_code.subject or self._label(client.client_id),
                           authorization_code.scopes, authorization_code.resource, key)

    def _issue(self, client_id: str, subject: str, scopes: list[str], resource: str | None, key: tuple[str, str],
               refresh_expires_at: float | None = None) -> OAuthToken:
        """Mint and save. Callers hold a _txn() (re-entrant) so nothing re-syncs between their edits and this save."""
        now = int(time.time())
        access, refresh = "wka_" + secrets.token_urlsafe(32), "wkr_" + secrets.token_urlsafe(32)
        rec = {"client_id": client_id, "subject": subject, "scopes": scopes, "resource": resource,
               "key": key[0], "key_hash": key[1]}
        with self._txn():
            self.state["access"][_hash(access)] = rec | {"expires_at": now + ACCESS_TTL}
            self.state["refresh"][_hash(refresh)] = rec | {"expires_at": refresh_expires_at or now + REFRESH_TTL}
            self._save()
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, scope=" ".join(scopes) or None, refresh_token=refresh)

    def _live(self, rec: dict | None) -> bool:
        """Unexpired, and the wk_ key that minted it still exists."""
        return bool(rec) and rec["expires_at"] > time.time() and self.tokens.hash_active(rec.get("key_hash", ""))

    # refresh
    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        self._sync()
        r = self.state["refresh"].get(_hash(refresh_token))
        if not self._live(r) or r["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=r["client_id"], scopes=r["scopes"], expires_at=r["expires_at"],
                            resource=r.get("resource"), subject=r["subject"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with self._txn():   # pop old, issue new and save in one locked section
            r = self.state["refresh"].pop(_hash(refresh_token.token), None)
            if not self._live(r):
                self._save()
                raise TokenError("invalid_grant", "unknown refresh token")
            # the lifetime is absolute: the new refresh token keeps the original expiry
            return self._issue(client.client_id, r["subject"], scopes or r["scopes"], r.get("resource"),
                               (r["key"], r["key_hash"]), refresh_expires_at=r["expires_at"])

    # access
    async def load_access_token(self, token: str) -> AccessToken | None:
        return self.resolve(token)

    def resolve(self, token: str | None) -> AccessToken | None:
        if not token:
            return None
        if token.startswith("wk_"):
            client = self.tokens.resolve(token)
            return AccessToken(token=token, client_id=f"key:{client}", scopes=[], subject=client) if client else None
        self._sync()
        a = self.state["access"].get(_hash(token))
        if not self._live(a):
            return None
        return AccessToken(token=token, client_id=a["client_id"], scopes=a["scopes"], expires_at=a["expires_at"],
                           resource=a.get("resource"), subject=a["subject"])

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._txn():
            self.state["access"].pop(_hash(token.token), None)
            self.state["refresh"].pop(_hash(token.token), None)
            self._save()

    def revoke_all(self) -> int:
        with self._txn():
            n = len(self.state["access"]) + len(self.state["refresh"])
            self.state["access"], self.state["refresh"] = {}, {}
            self._save()
            return n
