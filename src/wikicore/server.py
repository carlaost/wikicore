"""The MCP server: thin tool wrappers over Wiki. Descriptions come from DEFAULTS, the vault's
_tools.yaml and its _schema/ types, so an instance is customised without forking."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.applications import Starlette

from wikicore.custom import Custom, load_custom, param_overrides, tool_description
from wikicore.auth import TokenStore
from wikicore.oauth import OAuthProvider
from wikicore.vault import VaultError
from wikicore.web import register_routes
from wikicore.wiki import Wiki

log = logging.getLogger(__name__)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

DEFAULTS: dict[str, str] = {
    "bootstrap": "One-time setup per assistant: returns steps for filing what you already know about the owner's work "
                 "(your memory, past chats, local memory files) into this wiki. Call when the owner says 'bootstrap the wiki'. "
                 "Call again with done=true and a summary (and, if known, `created` / `updated` page counts) when finished. "
                 "`as_client` overrides which assistant you are (claude-code, claude-ai, chatgpt).",
    "search": "Search the wiki (titles, aliases, frontmatter, text). Use before answering about any person, org, "
              "project, paper or concept the user mentions. A few keywords, not a sentence; optionally filter by "
              "`type` or by `project` slug. Returns slugs for `get`. If nothing comes back, retry with synonyms or fewer words.",
    "get": "Read one page in full, with the slugs of pages that link to it. Unknown slugs return `did_you_mean`.",
    "related": "Pages one link away from a page, in both directions.",
    "list_notes": "List every page of one type, optionally filtered by frontmatter fields (exact match, or membership for list fields).",
    "recent": "The latest changes to the wiki, newest first.",
    "schema": "The page types, what each is for, their fields and sections, and the fields every page shares. Read before filing if unsure.",
    "status": "Wiki health: page counts per type, unfiled sources waiting to be filed, problems.",
    "save_source": "Save something verbatim before filing it: pasted meeting notes, a transcript, the text of a paper, a note. "
                   "Saved immediately. Returns a source_id, the existing pages it mentions, and what to do next "
                   "(`update` those pages and `file` new ones, passing the source_id to both).",
    "file": "Create a new page. Refuses if a page of that type with this title or alias exists and returns it instead: then "
            "use `update`. `similar` in the result lists near-matches to check. Leave `body` empty to get the type's section "
            "skeleton, or write the sections as '## Heading' blocks. Link pages as [[slug]]. Pass `source_id` when filing from a source.",
    "update": "Change an existing page: append to or replace one `section` (a ## heading, created if missing), and/or merge "
              "`frontmatter` (lists are unioned, null removes a field). Prefer this over creating pages. Pass `source_id` "
              "when the content comes from a saved source: it marks the source filed. Sources themselves cannot be edited.",
    "repos": "The code repos this wiki can read, with their project and when they were last fetched.",
    "register_repo": "Register a code repo (GitHub URL) so it can be read here; optionally link it to a `project` slug.",
    "repo_status": "Where a repo stands: branch, recent commits, README head, plus its project page's Status and Open sections.",
    "repo_read": "Read one file from a registered repo (text files, up to 200 kB).",
    "repo_log": "Recent commits of a registered repo, optionally for one path.",
    "repo_tree": "List files and folders in a registered repo, optionally under a path.",
    "add_type": "Add a page type when none of the existing ones fit. Give a `description` saying when to use it, optional "
                "`fields` (name: description) and `sections` (headings).",
}
TYPE_AWARE = ("search", "file", "update", "list_notes", "schema")


def types_line(wiki: Wiki) -> str:
    parts = [f"{s.name} ({s.description.split('.')[0]})" for s in wiki.vault.schemas.values() if s.name != "source"]
    return "Types: " + "; ".join(parts) + "."


def client_of(wiki: Wiki) -> str:
    if wiki.cfg.dev_client:
        return wiki.cfg.dev_client
    tok = get_access_token()
    return tok.subject if tok and tok.subject else "unknown"


def _run(f: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return f()
    except VaultError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001  a client never sees a stack trace
        log.exception("tool failed")
        return {"error": f"internal error: {type(e).__name__}: {e}"}


def _tools(wiki: Wiki) -> dict[str, tuple[Callable[..., Any], ToolAnnotations]]:
    def search(query: str, type: str | None = None, project: str | None = None, limit: int = 10) -> dict[str, Any]:
        return _run(lambda: wiki.search(query, type, project, limit))

    def get(slug: str) -> dict[str, Any]:
        return _run(lambda: wiki.get(slug))

    def related(slug: str) -> dict[str, Any]:
        return _run(lambda: wiki.related(slug))

    def list_notes(type: str, filters: dict[str, Any] | None = None, limit: int = 100) -> dict[str, Any]:
        return _run(lambda: wiki.list_notes(type, filters, limit))

    def recent(n: int = 20, type: str | None = None) -> dict[str, Any]:
        return _run(lambda: wiki.recent(n, type))

    def schema() -> dict[str, Any]:
        return _run(wiki.schema)

    def status() -> dict[str, Any]:
        return _run(wiki.status)

    def bootstrap(done: bool = False, summary: str | None = None, as_client: str | None = None,
                  created: int | None = None, updated: int | None = None) -> dict[str, Any]:
        return _run(lambda: wiki.bootstrap(client_of(wiki), as_client, done, summary, created, updated))

    def save_source(text: str, kind: str = "note", title: str | None = None, url: str | None = None) -> dict[str, Any]:
        return _run(lambda: wiki.save_source(text, kind, client_of(wiki), title, url))

    def file(type: str, title: str, frontmatter: dict[str, Any] | None = None, body: str = "",
             source_id: str | None = None) -> dict[str, Any]:
        return _run(lambda: wiki.file(type, title, client_of(wiki), frontmatter, body, source_id))

    def update(slug: str, section: str | None = None, content: str | None = None, mode: str = "append",
               frontmatter: dict[str, Any] | None = None, source_id: str | None = None) -> dict[str, Any]:
        return _run(lambda: wiki.update(slug, client_of(wiki), section, content, mode, frontmatter, source_id))

    def add_type(name: str, description: str, fields: dict[str, str] | None = None,
                 sections: list[str] | None = None, required: list[str] | None = None) -> dict[str, Any]:
        return _run(lambda: wiki.add_type(name, description, client_of(wiki), fields, sections, required))

    def repos() -> dict[str, Any]:
        return _run(wiki.repo_list)

    def register_repo(url: str, project: str | None = None, name: str | None = None) -> dict[str, Any]:
        return _run(lambda: wiki.register_repo(url, client_of(wiki), project, name))

    def repo_status(name: str) -> dict[str, Any]:
        return _run(lambda: wiki.repo_status(name))

    def repo_read(name: str, path: str) -> dict[str, Any]:
        return _run(lambda: wiki.repo_read(name, path))

    def repo_log(name: str, n: int = 20, path: str | None = None) -> dict[str, Any]:
        return _run(lambda: wiki.repo_log(name, n, path))

    def repo_tree(name: str, path: str = "") -> dict[str, Any]:
        return _run(lambda: wiki.repo_tree(name, path))

    repo_tools = {"repos": (repos, READ), "register_repo": (register_repo, WRITE), "repo_status": (repo_status, READ),
                  "repo_read": (repo_read, READ), "repo_log": (repo_log, READ), "repo_tree": (repo_tree, READ)}
    core = {
        "search": (search, READ), "get": (get, READ), "related": (related, READ), "list_notes": (list_notes, READ),
        "recent": (recent, READ), "schema": (schema, READ), "status": (status, READ),
        "bootstrap": (bootstrap, WRITE),
        "save_source": (save_source, WRITE), "file": (file, WRITE), "update": (update, WRITE), "add_type": (add_type, WRITE),
    }
    return core | (repo_tools if wiki.repos.enabled else {})


def _describe(wiki: Wiki, name: str, custom: Custom) -> str | None:
    d = tool_description(name, DEFAULTS[name], custom)
    if d is not None and name in TYPE_AWARE:
        d = f"{d} {types_line(wiki)}"
    return d


def refresh_descriptions(server: MCPServer, wiki: Wiki) -> None:
    custom = load_custom(wiki.root)
    for name in DEFAULTS:
        tool = server._tool_manager.get_tool(name)   # no public accessor in mcp 2.2; pinned by test_add_type_refreshes_descriptions
        if tool is None:
            continue
        d = _describe(wiki, name, custom)
        if d is not None:
            tool.description = d
        props = tool.parameters.get("properties", {})
        for p, text in param_overrides(name, custom).items():
            if p in props:
                props[p]["description"] = text


def make_oauth(wiki: Wiki) -> tuple[OAuthProvider, TokenStore]:
    tokens = TokenStore(wiki.cfg.state_dir / "tokens.json")
    return OAuthProvider(tokens, wiki.cfg.state_dir / "oauth.json", wiki.cfg.public_url), tokens


def build_server(wiki: Wiki, oauth: Any = None, tokens: TokenStore | None = None) -> MCPServer:
    custom = load_custom(wiki.root)

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[dict]:
        stop = asyncio.Event()

        async def syncer() -> None:
            while not stop.is_set():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), wiki.cfg.pull_interval)
                if stop.is_set():
                    break
                try:
                    result = await asyncio.to_thread(wiki.sync)
                    for err in result.get("errors", []):
                        log.warning("sync: %s", err)
                    if result.get("changed"):
                        refresh_descriptions(server, wiki)
                except Exception:  # noqa: BLE001
                    log.exception("periodic sync failed")

        task = asyncio.create_task(syncer()) if wiki.git.has_remote() else None
        try:
            yield {}
        finally:
            stop.set()
            if task:
                task.cancel()

    auth_kwargs: dict[str, Any] = {}
    if oauth is not None:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
        auth_kwargs = {"auth_server_provider": oauth, "auth": AuthSettings(
            issuer_url=wiki.cfg.public_url, resource_server_url=f"{wiki.cfg.public_url}/mcp",
            client_registration_options=ClientRegistrationOptions(enabled=True), validate_token_resource=False)}

    server = MCPServer(custom.name, instructions=custom.instructions, lifespan=lifespan, **auth_kwargs)
    for name, (fn, ann) in _tools(wiki).items():
        d = _describe(wiki, name, custom)
        if d is None:
            continue
        server.add_tool(fn, name=name, description=d, annotations=ann)
    refresh_descriptions(server, wiki)   # applies parameter overrides
    wiki.on_schema_change = lambda: refresh_descriptions(server, wiki)
    register_routes(server, wiki, oauth, tokens)
    return server


def build_asgi(wiki: Wiki) -> Starlette:
    oauth, tokens = (None, None) if wiki.cfg.dev_client else make_oauth(wiki)
    server = build_server(wiki, oauth, tokens)
    hosts = list(dict.fromkeys(wiki.cfg.allowed_hosts + [wiki.cfg.public_host]))
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts + [f"{h}:*" for h in hosts],
        allowed_origins=[f"https://{h}" for h in hosts] + [f"http://{h}" for h in hosts] + [f"http://{h}:*" for h in hosts],
    )
    return server.streamable_http_app(streamable_http_path="/mcp", transport_security=security, host=wiki.cfg.host)
