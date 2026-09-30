"""wikicore command line: init a vault, serve it, check it, manage client keys."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from wikicore.auth import TokenStore
from wikicore.config import Config
from wikicore.init_vault import init_vault
from wikicore.vault import Vault


def check_vault(root: Path) -> list[str]:
    """Every problem found in the vault: unreadable pages and schemas, schema warnings, dangling links.
    Never raises on a broken vault; an unexpected failure is reported as a problem."""
    try:
        v = Vault(root)
        return (list(v.problems) + [f"{n.slug}: {w}" for n in v.notes.values() for w in n.warnings]
                + [f"{src}: dangling link [[{dst}]]" for src, dst in v.dangling()])
    except Exception as e:  # noqa: BLE001  check exists to describe a broken vault, not to crash on it
        return [f"vault could not be read: {e}"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wikicore")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="create a vault from the defaults (adds only missing files)")
    i.add_argument("dir")
    sub.add_parser("serve", help="run the MCP server (streamable HTTP on /mcp)")
    sub.add_parser("check", help="report unreadable pages, schema warnings and dangling links")
    t = sub.add_parser("token", help="manage client keys")
    t.add_argument("action", choices=["add", "list", "revoke"])
    t.add_argument("client", nargs="?")
    o = sub.add_parser("oauth", help="manage OAuth sessions")
    o.add_argument("action", choices=["revoke-all"])
    a = p.parse_args(argv)

    if a.cmd == "init":
        print(init_vault(Path(a.dir)))
        return 0
    cfg = Config.from_env()
    if a.cmd == "check":
        problems = check_vault(cfg.vault)
        print("\n".join(problems) or "ok")
        return 1 if problems else 0
    if a.cmd == "token":
        store = TokenStore(cfg.state_dir / "tokens.json")
        if a.action == "list":
            print("\n".join(store.list()))
            return 0
        if not a.client:
            p.error("token add/revoke needs a client name")
        if a.action == "add":
            print(store.add(a.client))
            return 0
        return 0 if store.revoke(a.client) else 1
    if a.cmd == "oauth":
        from wikicore.oauth import OAuthProvider
        provider = OAuthProvider(TokenStore(cfg.state_dir / "tokens.json"), cfg.state_dir / "oauth.json", cfg.public_url)
        print(provider.revoke_all(), "tokens revoked")
        return 0
    if a.cmd == "serve":
        import uvicorn

        from wikicore.server import build_asgi
        from wikicore.wiki import Wiki
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        if not cfg.dev_client and not TokenStore(cfg.state_dir / "tokens.json").list():
            print("no client keys yet: run `wikicore token add owner` first", file=sys.stderr)
            return 1
        # Single process on purpose: OAuth pending requests and codes live in memory, per process.
        uvicorn.run(build_asgi(Wiki(cfg)), host=cfg.host, port=cfg.port, workers=1,
                    proxy_headers=True, forwarded_allow_ips="127.0.0.1")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
