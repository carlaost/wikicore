"""Plain web pages: health, OAuth login, upload, connect guide. No framework, no JS build."""

from __future__ import annotations

import html
from typing import Any

from mcp.server.auth.provider import AuthorizeError
from mcp.server.mcpserver import MCPServer
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from wikicore.auth import TokenStore
from wikicore.custom import load_custom
from wikicore.vault import VaultError, slugify
from wikicore.wiki import MAX_UPLOAD, UploadTooLarge, Wiki

STYLE = """<style>body{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 16px;
background:#fafafa;color:#222}input,textarea,button{font:inherit;width:100%;margin:.4rem 0;padding:.5rem;
box-sizing:border-box}button{background:#222;color:#fff;border:0;border-radius:6px}code,pre{background:#eee;
padding:.1rem .3rem;border-radius:4px;overflow-x:auto}pre{padding:.6rem}.err{color:#b00}
@media (prefers-color-scheme:dark){body{background:#161616;color:#eee}code,pre{background:#2a2a2a}
button{background:#eee;color:#111}}</style>"""


SECURITY_HEADERS = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'",
                    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(headers=SECURITY_HEADERS, content=f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                        f"<title>{html.escape(title)}</title>{STYLE}<h1>{html.escape(title)}</h1>{body}")


class _BodyTooLarge(Exception):
    pass


def _bounded(receive, limit: int):
    """Wrap an ASGI receive so reading more than `limit` body bytes raises instead of spooling more to disk."""
    seen = 0

    async def wrapped():
        nonlocal seen
        msg = await receive()
        if msg["type"] == "http.request":
            seen += len(msg.get("body", b""))
            if seen > limit:
                raise _BodyTooLarge
        return msg
    return wrapped


def register_routes(server: MCPServer, wiki: Wiki, oauth: Any, tokens: TokenStore | None) -> None:
    name = lambda: load_custom(wiki.root).name  # noqa: E731  re-read so a vault edit shows without restart

    slug_name = lambda: slugify(name())  # noqa: E731

    @server.custom_route("/", methods=["GET"])
    async def connect(_: Request) -> HTMLResponse:
        url = f"{wiki.cfg.public_url}/mcp"
        e = html.escape
        upload_link = "<p><a href=/upload>Upload a file</a></p>" if tokens else ""
        return page(name(), f"""
<p>An MCP server. Connect your assistant to <code>{e(url)}</code>.</p>
<h2>claude.ai / Claude Desktop</h2><p>Settings, Connectors, Add custom connector, then paste the URL. Log in with your key.</p>
<h2>ChatGPT</h2><p>Settings, Apps &amp; Connectors, Advanced, turn Developer mode on, Create, paste the URL and choose OAuth. Log in with your key.</p>
<h2>Claude Code</h2><pre>claude mcp add --scope user --transport http {e(slug_name())} {e(url)} \\
  --header "Authorization: Bearer $YOUR_WK_KEY"</pre>
<h2>Codex and other CLI agents</h2><p>Add an HTTP MCP server with this URL and the header <code>Authorization: Bearer wk_...</code>.</p>{upload_link}""")

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    if oauth is None:
        return

    @server.custom_route("/login", methods=["GET", "POST"])
    async def login(request: Request):
        rid = request.query_params.get("req", "")
        client = oauth.pending_client(rid)
        if not client:
            return page(name(), "<p class=err>This login link has expired. Start again from your AI client.</p>")
        error = ""
        if request.method == "POST":
            form = await request.form()
            try:
                return RedirectResponse(oauth.complete_login(rid, str(form.get("key", "")).strip()), status_code=302,
                                        headers=SECURITY_HEADERS)
            except AuthorizeError as e:
                error = f"<p class=err>{html.escape(e.error_description or 'login failed')}</p>"
        host = html.escape(oauth.pending_redirect_host(rid) or "unknown")
        return page(name(), f"<p>Connect <b>{html.escape(client)}</b> to this wiki.</p>"
                            f"<p>After you connect, you will be sent to <b>{host}</b>. Only continue if you trust it.</p>{error}"
                            f"<form method=post><input name=key type=password placeholder='your wk_ key' autocomplete=current-password>"
                            f"<button>Connect</button></form>")

    @server.custom_route("/upload", methods=["GET", "POST"])
    async def upload(request: Request):
        title = f"{name()}: upload"
        form_html = ("<form method=post enctype=multipart/form-data><input name=key type=password placeholder='your wk_ key' "
                     "autocomplete=current-password><input name=file type=file required><textarea name=note rows=3 "
                     "placeholder='optional: what this is, why it matters'></textarea><button>Upload</button></form>")

        def fail(msg: str, status: int) -> HTMLResponse:
            r = page(title, f"<p class=err>{html.escape(msg)}</p>" + form_html)
            r.status_code = status
            return r

        if request.method == "GET":
            return page(title, form_html)
        raw = request.headers.get("content-length")
        if raw is None:
            return fail("Uploads need a Content-Length header.", 411)
        try:
            declared = int(raw)
        except ValueError:
            return fail("The upload could not be read. Try again.", 400)
        limit = MAX_UPLOAD + 1024 * 1024
        if declared < 0:
            return fail("The upload could not be read. Try again.", 400)
        if declared > limit:
            return fail("That file is larger than 25 MB.", 413)
        # never trust the header alone: stop reading once the body passes the declared size or the cap
        request = Request(request.scope, _bounded(request.receive, min(declared, limit)))
        try:
            async with request.form() as form:
                client = tokens.resolve(str(form.get("key", "")).strip()) if tokens else None
                if not client:
                    return fail("That key is not valid.", 403)
                f = form.get("file")
                if f is None or isinstance(f, str) or not f.filename:
                    return fail("Choose a file.", 400)
                if (f.size or 0) > MAX_UPLOAD:
                    return fail("That file is larger than 25 MB.", 413)
                data = await f.read()
                note = str(form.get("note", ""))
                filename = f.filename
        except _BodyTooLarge:
            return fail("That file is larger than 25 MB.", 413)
        except Exception:  # noqa: BLE001  a malformed body must give a plain message, not a stack trace
            return fail("The upload could not be read. Try again.", 400)
        try:
            r = await run_in_threadpool(wiki.save_upload, filename, data, note, client)
        except UploadTooLarge as e:
            return fail(str(e), 413)
        except VaultError as e:
            return fail(str(e), 400)
        except Exception:  # noqa: BLE001
            return fail("The file could not be saved. Try again.", 500)
        return page(title, f"<p>Saved as <code>{html.escape(r['source_id'])}</code>. "
                           f"Your assistant will offer to file it next time.</p>" + form_html)
