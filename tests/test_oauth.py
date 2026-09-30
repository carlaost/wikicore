import base64
import hashlib
import time

import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from mcp.shared.auth import OAuthClientInformationFull
from starlette.testclient import TestClient

from wikicore.auth import TokenStore
from wikicore.config import Config
from wikicore.oauth import OAuthProvider
from wikicore.server import build_asgi
from wikicore.wiki import Wiki


@pytest.fixture
def tokens(tmp_path):
    return TokenStore(tmp_path / "state" / "tokens.json")


@pytest.fixture
def provider(tokens, tmp_path):
    return OAuthProvider(tokens, tmp_path / "state" / "oauth.json", "https://wiki.example")


def client_info(name="ChatGPT"):
    return OAuthClientInformationFull(client_id="cid", client_name=name, redirect_uris=["https://client.example/cb"])


def params():
    return AuthorizationParams(state="s1", scopes=[], code_challenge="x" * 43, redirect_uri="https://client.example/cb",
                               redirect_uri_provided_explicitly=True, resource=None)


def test_token_store(tokens, tmp_path):
    key = tokens.add("Owner")
    assert key.startswith("wk_") and tokens.resolve(key) == "owner"
    assert tokens.resolve("wk_wrong") is None and tokens.list() == ["owner"]
    assert "wk_" not in (tmp_path / "state" / "tokens.json").read_text()   # only hashes on disk
    assert tokens.revoke("owner") and tokens.resolve(key) is None


async def test_full_oauth_flow(provider, tokens):
    key = tokens.add("owner")
    c = client_info()
    await provider.register_client(c)
    login_url = await provider.authorize(c, params())
    rid = login_url.split("req=")[1]
    assert provider.pending_client(rid) == "ChatGPT"
    with pytest.raises(AuthorizeError):
        provider.complete_login(rid, "wk_wrong")
    redirect = provider.complete_login(rid, key)
    assert redirect.startswith("https://client.example/cb?code=") and "state=s1" in redirect
    code = redirect.split("code=")[1].split("&")[0]
    ac = await provider.load_authorization_code(c, code)
    tok = await provider.exchange_authorization_code(c, ac)
    at = await provider.load_access_token(tok.access_token)
    assert at.subject == "chatgpt"
    rt = await provider.load_refresh_token(c, tok.refresh_token)
    tok2 = await provider.exchange_refresh_token(c, rt, [])
    assert (await provider.load_access_token(tok2.access_token)).subject == "chatgpt"
    assert await provider.load_refresh_token(c, tok.refresh_token) is None   # rotated


async def test_bearer_key_is_an_access_token(provider, tokens):
    key = tokens.add("claude-code")
    assert (await provider.load_access_token(key)).subject == "claude-code"
    assert await provider.load_access_token("nope") is None


async def test_state_survives_restart(provider, tokens, tmp_path):
    await provider.register_client(client_info())
    again = OAuthProvider(tokens, tmp_path / "state" / "oauth.json", "https://wiki.example")
    assert (await again.get_client("cid")).client_name == "ChatGPT"


def test_login_page_and_unauthenticated_mcp(vault_dir, tmp_path):
    cfg = Config(vault=vault_dir, state_dir=tmp_path / "state", git_push=False, public_url="https://testserver",
                 allowed_hosts=["testserver"])
    with TestClient(build_asgi(Wiki(cfg))) as http:
        assert http.get("/health").json() == {"ok": True}
        assert http.post("/mcp", json={}).status_code == 401
        assert "expired" in http.get("/login?req=nope").text


def test_secret_files_are_private(tokens, tmp_path):
    tokens.add("owner")
    assert (tmp_path / "state" / "tokens.json").stat().st_mode & 0o777 == 0o600
    assert not list((tmp_path / "state").glob("*.tmp"))


async def test_oauth_state_private_and_codes_single_use(provider, tokens, tmp_path):
    key = tokens.add("owner")
    c = client_info()
    await provider.register_client(c)
    assert (tmp_path / "state" / "oauth.json").stat().st_mode & 0o777 == 0o600
    rid = (await provider.authorize(c, params())).split("req=")[1]
    code = provider.complete_login(rid, key).split("code=")[1].split("&")[0]
    ac = await provider.load_authorization_code(c, code)
    await provider.exchange_authorization_code(c, ac)
    assert await provider.load_authorization_code(c, code) is None
    from mcp.server.auth.provider import TokenError
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(c, ac)
    with pytest.raises(AuthorizeError):
        provider.complete_login(rid, key)   # login request is consumed


async def test_expired_code_rejected(provider, tokens):
    key = tokens.add("owner")
    c = client_info()
    await provider.register_client(c)
    rid = (await provider.authorize(c, params())).split("req=")[1]
    code = provider.complete_login(rid, key).split("code=")[1].split("&")[0]
    provider.codes[code].expires_at = time.time() - 1
    assert await provider.load_authorization_code(c, code) is None


async def test_revoked_key_stops_working_immediately(provider, tokens):
    key = tokens.add("claude-code")
    assert await provider.load_access_token(key)
    tokens.revoke("claude-code")
    assert await provider.load_access_token(key) is None


def test_malformed_tokens_file_is_tolerated(tokens):
    tokens.path.parent.mkdir(parents=True)
    tokens.path.write_text("{not json", encoding="utf-8")
    assert tokens.resolve("wk_x") is None and tokens.list() == []


def test_login_page_never_echoes_key(vault_dir, tmp_path):
    cfg = Config(vault=vault_dir, state_dir=tmp_path / "state", git_push=False, public_url="https://testserver",
                 allowed_hosts=["testserver"])
    wiki = Wiki(cfg)
    with TestClient(build_asgi(wiki)) as http:
        reg = http.post("/register", json={"client_name": "Ada's Engine", "redirect_uris": ["https://client.example/cb"],
                                           "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
                                           "response_types": ["code"]})
        assert reg.status_code == 201, reg.text
        cid = reg.json()["client_id"]
        verifier = "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        q = {"response_type": "code", "client_id": cid, "redirect_uri": "https://client.example/cb", "state": "s",
             "code_challenge": challenge, "code_challenge_method": "S256"}
        bad = http.get("/authorize", params=q | {"redirect_uri": "https://evil.example/cb"}, follow_redirects=False)
        assert bad.status_code == 400
        nopkce = http.get("/authorize", params={k: v for k, v in q.items() if not k.startswith("code_challenge")}, follow_redirects=False)
        assert "code=" not in nopkce.headers.get("location", "")
        r = http.get("/authorize", params=q, follow_redirects=False)
        login = r.headers["location"]
        assert "Ada" in http.get(login).text
        secret = "wk_definitely-wrong-<script>"
        page = http.post(login, data={"key": secret}, follow_redirects=False)
        assert page.status_code == 200 and "wk_definitely" not in page.text and "script" not in page.text.split("not valid")[-1]
        key = TokenStore(tmp_path / "state" / "tokens.json").add("owner")
        ok = http.post(login, data={"key": key}, follow_redirects=False)
        assert ok.status_code == 302 and ok.headers["location"].startswith("https://client.example/cb?code=")
        code = ok.headers["location"].split("code=")[1].split("&")[0]
        form = {"grant_type": "authorization_code", "code": code, "client_id": cid, "redirect_uri": "https://client.example/cb"}
        assert http.post("/token", data=form | {"code_verifier": "w" * 64}).status_code == 400   # wrong PKCE verifier
        good = http.post("/token", data=form | {"code_verifier": verifier})
        assert good.status_code == 200 and good.json()["refresh_token"]
        assert http.post("/token", data=form | {"code_verifier": verifier}).status_code == 400   # replay of a used code


async def _login(provider, tokens, label="owner", name="ChatGPT"):
    key = tokens.add(label)
    c = client_info(name)
    await provider.register_client(c)
    rid = (await provider.authorize(c, params())).split("req=")[1]
    code = provider.complete_login(rid, key).split("code=")[1].split("&")[0]
    tok = await provider.exchange_authorization_code(c, await provider.load_authorization_code(c, code))
    return c, key, tok


async def test_revoke_all_from_another_instance_and_save_does_not_resurrect(provider, tokens, tmp_path):
    c, _, tok = await _login(provider, tokens)
    other = OAuthProvider(tokens, tmp_path / "state" / "oauth.json", "https://wiki.example")
    assert other.revoke_all() == 2
    assert await provider.load_access_token(tok.access_token) is None
    assert await provider.load_refresh_token(c, tok.refresh_token) is None
    await provider.register_client(OAuthClientInformationFull(client_id="c2", client_name="x", redirect_uris=["https://a.example/cb"]))
    assert await other.load_access_token(tok.access_token) is None
    assert await provider.load_access_token(tok.access_token) is None


async def test_revoking_key_kills_its_oauth_tokens(provider, tokens):
    c, _, tok = await _login(provider, tokens, "owner")
    assert await provider.load_access_token(tok.access_token)
    tokens.revoke("owner")
    assert await provider.load_access_token(tok.access_token) is None
    assert await provider.load_refresh_token(c, tok.refresh_token) is None
    tokens.add("owner")   # same label, new key: old sessions stay dead
    assert await provider.load_access_token(tok.access_token) is None


async def test_tokens_hashed_at_rest_and_refresh_expiry_absolute(provider, tokens, tmp_path):
    c, _, tok = await _login(provider, tokens)
    text = (tmp_path / "state" / "oauth.json").read_text(encoding="utf-8")
    assert tok.access_token not in text and tok.refresh_token not in text
    rt = await provider.load_refresh_token(c, tok.refresh_token)
    tok2 = await provider.exchange_refresh_token(c, rt, [])
    rt2 = await provider.load_refresh_token(c, tok2.refresh_token)
    assert rt2.expires_at == rt.expires_at


async def test_redirect_uri_validation_and_fragment(provider, tokens):
    def reg(uri):
        return provider.register_client(OAuthClientInformationFull(client_id="r", redirect_uris=[uri]))
    from mcp.server.auth.provider import RegistrationError
    for bad in ("http://evil.example/cb", "https://a.example/cb#frag", "myapp://cb"):
        with pytest.raises(RegistrationError):
            await reg(bad)
    await reg("http://localhost:3000/cb")
    await reg("http://127.0.0.1/cb")
    key = tokens.add("owner")
    c = client_info()
    await provider.register_client(c)
    p = params()
    p.redirect_uri = "https://client.example/cb?x=1"
    rid = (await provider.authorize(c, p)).split("req=")[1]
    url = provider.complete_login(rid, key)
    assert url.startswith("https://client.example/cb?x=1&code=") and "state=s1" in url


async def test_client_pruning_and_caps(provider, tokens, monkeypatch):
    from mcp.server.auth.provider import RegistrationError
    from wikicore import oauth as o
    await provider.register_client(client_info())
    provider.state["client_meta"]["cid"]["created"] = time.time() - 7200
    await provider.register_client(OAuthClientInformationFull(client_id="c2", redirect_uris=["https://a.example/cb"]))
    assert await provider.get_client("cid") is None   # unused and old: pruned
    monkeypatch.setattr(o, "MAX_CLIENTS", 2)
    await provider.register_client(OAuthClientInformationFull(client_id="c3", redirect_uris=["https://a.example/cb"]))
    with pytest.raises(RegistrationError):
        await provider.register_client(OAuthClientInformationFull(client_id="c4", redirect_uris=["https://a.example/cb"]))
    monkeypatch.setattr(o, "MAX_PENDING", 1)
    c = await provider.get_client("c2")
    await provider.authorize(c, params())
    with pytest.raises(AuthorizeError):
        await provider.authorize(c, params())


async def test_used_client_is_not_pruned(provider, tokens):
    c, _, _ = await _login(provider, tokens)
    provider.state["client_meta"]["cid"]["created"] = time.time() - 7200
    await provider.register_client(OAuthClientInformationFull(client_id="c2", redirect_uris=["https://a.example/cb"]))
    assert await provider.get_client("cid") is not None


def test_login_page_shows_host_and_has_security_headers(vault_dir, tmp_path):
    cfg = Config(vault=vault_dir, state_dir=tmp_path / "state", git_push=False, public_url="https://testserver",
                 allowed_hosts=["testserver"])
    with TestClient(build_asgi(Wiki(cfg))) as http:
        cid = http.post("/register", json={"client_name": "Ada", "redirect_uris": ["https://client.example/cb"],
                                           "token_endpoint_auth_method": "none"}).json()["client_id"]
        r = http.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": "https://client.example/cb",
                                           "code_challenge": "x" * 43, "code_challenge_method": "S256"}, follow_redirects=False)
        page = http.get(r.headers["location"])
        assert "<b>client.example</b>" in page.text
        assert page.headers["x-frame-options"] == "DENY" and page.headers["cache-control"] == "no-store"
        assert page.headers["content-security-policy"] == "frame-ancestors 'none'"
        assert page.headers["referrer-policy"] == "no-referrer"
        bad = http.post("/register", json={"redirect_uris": ["http://evil.example/cb"]})
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_redirect_uri"


async def test_refresh_rotation_survives_external_write_mid_exchange(provider, tokens, tmp_path, monkeypatch):
    c, _, tok = await _login(provider, tokens)
    rt = await provider.load_refresh_token(c, tok.refresh_token)
    other = OAuthProvider(tokens, tmp_path / "state" / "oauth.json", "https://wiki.example")
    orig = provider._issue

    def issue_after_external_write(*a, **kw):
        other.state["clients"]["zz"] = {"client_id": "zz"}   # an external process rewrites the file first
        other._save()
        return orig(*a, **kw)

    monkeypatch.setattr(provider, "_issue", issue_after_external_write)
    tok2 = await provider.exchange_refresh_token(c, rt, [])
    assert await provider.load_refresh_token(c, tok.refresh_token) is None
    fresh = OAuthProvider(tokens, tmp_path / "state" / "oauth.json", "https://wiki.example")
    assert await fresh.load_refresh_token(c, tok.refresh_token) is None
    assert await fresh.load_refresh_token(c, tok2.refresh_token) is not None


def test_file_lock_blocks_other_processes(tmp_path):
    import subprocess, sys
    from wikicore.auth import file_lock
    target = tmp_path / "state" / "x.json"
    code = ("import sys;from pathlib import Path;from wikicore.auth import file_lock\n"
            "import fcntl,os\n"
            "fd=os.open(str(Path(sys.argv[1]).with_name('x.json.lock')),os.O_WRONLY|os.O_CREAT)\n"
            "try:\n fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);print('got')\n"
            "except BlockingIOError:\n print('blocked')\n")
    with file_lock(target):
        with file_lock(target):   # re-entrant
            out = subprocess.run([sys.executable, "-c", code, str(target)], capture_output=True, text=True).stdout.strip()
    assert out == "blocked"
    out = subprocess.run([sys.executable, "-c", code, str(target)], capture_output=True, text=True).stdout.strip()
    assert out == "got"


async def test_userinfo_redirect_rejected_and_host_display(provider, tokens):
    from mcp.server.auth.provider import RegistrationError
    with pytest.raises(RegistrationError):
        await provider.register_client(OAuthClientInformationFull(client_id="u", redirect_uris=["https://chatgpt.com@evil.example/cb"]))
    c = client_info()
    await provider.register_client(c)
    p = params()
    p.redirect_uri = "https://client.example:8443/cb"
    rid = (await provider.authorize(c, p)).split("req=")[1]
    assert provider.pending_redirect_host(rid) == "client.example:8443"
