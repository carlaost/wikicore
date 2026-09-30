import pytest
from starlette.testclient import TestClient

from wikicore.auth import TokenStore
from wikicore.config import Config
from wikicore.server import build_asgi
from wikicore.wiki import Wiki, extract_text


def make_pdf(text: str) -> bytes:
    stream = f"BT /F1 18 Tf 20 60 Td ({text}) Tj ET".encode()
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 144]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
            b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj" % i + o + b"endobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def test_extract_text():
    assert "Hello engines" in extract_text("paper.pdf", make_pdf("Hello engines"))
    assert extract_text("notes.md", "# hi".encode()) == "# hi"
    assert extract_text("photo.jpg", b"\xff\xd8") == ""


def test_save_upload(vault_dir, tmp_path):
    wiki = Wiki(Config(vault=vault_dir, state_dir=tmp_path / "s", git_push=False, dev_client="t"))
    r = wiki.save_upload("On Engines (1843).pdf", make_pdf("Hello engines"), "read this", "upload")
    src = wiki.vault.get(r["source_id"])
    assert src.meta["added_by"] == "upload"
    assert src.meta["kind"] == "upload" and src.meta["filed"] is False
    assert (vault_dir / src.meta["file"]).read_bytes().startswith(b"%PDF")
    assert "read this" in src.body and "Hello engines" in src.body
    assert r["source_id"] in {u["source_id"] for u in wiki.status()["unfiled_sources"]}


@pytest.fixture
def http(vault_dir, tmp_path):
    cfg = Config(vault=vault_dir, state_dir=tmp_path / "state", git_push=False, public_url="https://testserver",
                 allowed_hosts=["testserver"])
    key = TokenStore(tmp_path / "state" / "tokens.json").add("owner")
    with TestClient(build_asgi(Wiki(cfg))) as c:
        yield c, key


def test_upload_page(http):
    c, key = http
    assert "<form" in c.get("/upload").text
    bad = c.post("/upload", data={"key": "wk_nope", "note": ""}, files={"file": ("a.pdf", make_pdf("x"), "application/pdf")})
    assert bad.status_code == 403
    ok = c.post("/upload", data={"key": key, "note": "for later"}, files={"file": ("a.pdf", make_pdf("Hello"), "application/pdf")})
    assert ok.status_code == 200 and "Saved" in ok.text


def test_connect_page(http):
    c, _ = http
    text = c.get("/").text
    assert "https://testserver/mcp" in text and "claude mcp add" in text


def _post(c, key, name, data, note="", ctype="application/octet-stream"):
    return c.post("/upload", data={"key": key, "note": note}, files={"file": (name, data, ctype)})


def test_upload_uses_client_from_key(http, vault_dir):
    c, key = http
    r = _post(c, key, "a.txt", b"plain words", "n")
    assert r.status_code == 200
    src = [p for p in (vault_dir / "sources").glob("*.md")] if (vault_dir / "sources").exists() else []
    assert any("added_by: owner" in p.read_text() for p in src)


def test_upload_rejects_missing_key_and_never_echoes(http):
    c, key = http
    r = _post(c, "", "a.txt", b"x")
    assert r.status_code == 403 and "not valid" in r.text and "Traceback" not in r.text
    r = _post(c, "wk_secretguess", "a.txt", b"x")
    assert "wk_secretguess" not in r.text
    assert 'type=password' in c.get("/upload").text
    assert "frame-ancestors" in c.get("/upload").headers["content-security-policy"]
    assert c.get("/").headers["x-frame-options"] == "DENY"


def test_upload_too_large(http, monkeypatch):
    import wikicore.web as web
    monkeypatch.setattr(web, "MAX_UPLOAD", 10)
    c, key = http
    r = _post(c, key, "a.txt", b"x" * 100)
    assert r.status_code == 413 and "25 MB" in r.text


def test_upload_filename_cannot_escape_and_collisions(http, vault_dir):
    c, key = http
    for _ in range(2):
        assert _post(c, key, "../../etc/pass wd.TXT", b"hello").status_code == 200
    files = [p for p in (vault_dir / "_files").rglob("*") if p.is_file()]
    assert len(files) == 2 and len({p.parent for p in files}) == 2
    assert all(p.name == "pass-wd.txt" for p in files)
    assert not (vault_dir.parent / "etc").exists()


def test_corrupt_pdf_is_still_stored(vault_dir, tmp_path):
    wiki = Wiki(Config(vault=vault_dir, state_dir=tmp_path / "s", git_push=False, dev_client="t"))
    r = wiki.save_upload("bad.pdf", b"%PDF-1.4 garbage", "", "owner")
    src = wiki.vault.get(r["source_id"])
    assert "extraction failed" in src.body
    assert (vault_dir / r["file"]).read_bytes() == b"%PDF-1.4 garbage"


def test_upload_requires_content_length(http):
    c, key = http

    def chunks():
        yield b"--x\r\n"
    r = c.post("/upload", content=chunks(), headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 411 and "Traceback" not in r.text


def test_upload_oversize_header_rejected_before_parsing(http, monkeypatch):
    import wikicore.web as web
    monkeypatch.setattr(web, "MAX_UPLOAD", 10)
    c, _ = http
    r = c.post("/upload", content=b"x" * (2 * 1024 * 1024), headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413


def test_upload_body_longer_than_declared_is_cut_off(http, monkeypatch):
    import wikicore.web as web
    seen = []
    orig = web._bounded

    def spy(receive, limit):
        seen.append(limit)
        return orig(receive, limit)
    monkeypatch.setattr(web, "_bounded", spy)
    c, key = http
    assert _post(c, key, "a.txt", b"ok").status_code == 200 and seen
    # the wrapper itself aborts once more than the limit has been read
    import asyncio

    async def go():
        async def recv():
            return {"type": "http.request", "body": b"x" * 20, "more_body": True}
        with pytest.raises(web._BodyTooLarge):
            await orig(recv, 10)()
    asyncio.run(go())
