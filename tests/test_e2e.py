"""End to end, in process: OAuth (register, login with a key, token) then an MCP session that files a
pasted note, over a real vault and a bare local git remote."""

import base64
import hashlib
import json
import subprocess

import pytest
from starlette.testclient import TestClient

from conftest import git
from wikicore.auth import TokenStore
from wikicore.config import Config
from wikicore.server import build_asgi
from wikicore.vault import Vault
from wikicore.wiki import Wiki

CB = "https://client.example/cb"
NOTE = "Call with Ada Lovelace and Charles Babbage about the Analytical Engine.\n\nAda owes notes.  \n"


def rpc(http, tok, method, params=None, sid=None, rid=1):
    h = {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream",
         "Content-Type": "application/json"}
    if sid:
        h["mcp-session-id"] = sid
    body = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
    if rid is not None:
        body["id"] = rid
    r = http.post("/mcp", headers=h, content=json.dumps(body))
    txt = r.text
    if "data:" in txt:
        txt = [line[5:].strip() for line in txt.splitlines() if line.startswith("data:")][-1]
    return r, (json.loads(txt) if txt.strip() else None)


def mcp_session(http, tok):
    r, _ = rpc(http, tok, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "t", "version": "1"}})
    assert r.status_code == 200, r.text
    sid = r.headers["mcp-session-id"]
    rpc(http, tok, "notifications/initialized", sid=sid, rid=None)
    n = [1]

    def call(name, args):
        n[0] += 1
        _, j = rpc(http, tok, "tools/call", {"name": name, "arguments": args}, sid=sid, rid=n[0])
        res = j["result"]
        sc = res.get("structuredContent")
        return sc.get("result", sc) if sc else json.loads(res["content"][0]["text"])
    return call


def oauth_token(http, key):
    reg = http.post("/register", json={"client_name": "ChatGPT", "redirect_uris": [CB],
                                       "token_endpoint_auth_method": "none",
                                       "grant_types": ["authorization_code", "refresh_token"],
                                       "response_types": ["code"]})
    cid = reg.json()["client_id"]
    ver = "v" * 64
    ch = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).rstrip(b"=").decode()
    q = {"response_type": "code", "client_id": cid, "redirect_uri": CB, "state": "s",
         "code_challenge": ch, "code_challenge_method": "S256"}
    login = http.get("/authorize", params=q, follow_redirects=False).headers["location"]
    ok = http.post(login, data={"key": key}, follow_redirects=False)
    code = ok.headers["location"].split("code=")[1].split("&")[0]
    r = http.post("/token", data={"grant_type": "authorization_code", "code": code, "client_id": cid,
                                  "redirect_uri": CB, "code_verifier": ver})
    return r.json()["access_token"]


def test_oauth_to_mcp_filing_flow(vault_dir, tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(vault_dir, "remote", "add", "origin", str(bare))
    git(vault_dir, "push", "-q", "origin", "HEAD")
    cfg = Config(vault=vault_dir, state_dir=tmp_path / "state", git_push=True, public_url="https://testserver",
                 allowed_hosts=["testserver"])
    key = TokenStore(tmp_path / "state" / "tokens.json").add("owner")

    with TestClient(build_asgi(Wiki(cfg))) as http:
        call = mcp_session(http, oauth_token(http, key))

        s = call("save_source", {"text": NOTE, "kind": "granola", "title": "Ada call"})
        assert s["saved"] and s["source_id"]
        ada = call("file", {"type": "person", "title": "Ada Lovelace", "frontmatter": {"aliases": ["Ada"]}})
        assert ada["created"] and ada["slug"] == "ada-lovelace"
        m = call("file", {"type": "meeting", "title": "2026-01-31 Ada Lovelace", "source_id": s["source_id"],
                          "frontmatter": {"date": "2026-01-31", "with": ["[[ada-lovelace]]"]},
                          "body": "## Summary\n\nEngine talk."})
        assert m["created"]
        u = call("update", {"slug": "ada-lovelace", "section": "Open threads", "content": "- Ada owes notes"})
        assert u["updated"]

        dup = call("file", {"type": "person", "title": "Ada"})
        assert dup["created"] is False and dup["existing"]["slug"] == "ada-lovelace"
        err = call("update", {"slug": "ada-lovelace", "mode": "zap", "content": "x"})
        assert set(err) == {"error"} and isinstance(err["error"], str)
        assert call("status", {})["unfiled_sources"] == []

    authors = set(git(vault_dir, "log", "--format=%an", "-n", "4").split())
    assert authors == {"chatgpt"}
    src = vault_dir / "sources" / f"{s['source_id']}.md"
    assert src.read_bytes().endswith(NOTE.encode())
    stored = Vault(vault_dir).get(s["source_id"])
    assert stored.meta["filed"] is True and "[[2026-01-31-ada-lovelace]]" in stored.meta["filed_into"]
    assert "Ada owes notes" in Vault(vault_dir).get("ada-lovelace").body
    assert git(vault_dir, "rev-parse", "HEAD") == git(bare, "rev-parse", "HEAD")   # pushed to the remote
