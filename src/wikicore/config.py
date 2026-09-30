"""Runtime configuration, from WIKICORE_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class Config:
    vault: Path
    state_dir: Path
    public_url: str = "http://127.0.0.1:8050"
    host: str = "127.0.0.1"
    port: int = 8050
    git_push: bool = True
    pull_interval: int = 300
    repo_ttl: int = 300
    github_token: str | None = None
    dev_client: str | None = None   # no auth; every call acts as this client (local development only)
    allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1", "localhost"])

    @property
    def public_host(self) -> str:
        return urlparse(self.public_url).hostname or "127.0.0.1"

    @classmethod
    def from_env(cls) -> Config:
        e = os.environ.get
        vault = Path(e("WIKICORE_VAULT", "./vault")).expanduser().resolve()
        return cls(
            vault=vault,
            state_dir=Path(e("WIKICORE_STATE", str(vault.parent / "state"))).expanduser().resolve(),
            public_url=e("WIKICORE_PUBLIC_URL", "http://127.0.0.1:8050"),
            host=e("WIKICORE_HOST", "127.0.0.1"),
            port=int(e("WIKICORE_PORT", "8050")),
            git_push=e("WIKICORE_GIT_PUSH", "1") not in ("0", "false", "no"),
            pull_interval=int(e("WIKICORE_PULL_INTERVAL", "300")),
            repo_ttl=int(e("WIKICORE_REPO_TTL", "300")),
            github_token=e("WIKICORE_GITHUB_TOKEN") or None,
            dev_client=e("WIKICORE_DEV_CLIENT") or None,
            allowed_hosts=[h for h in e("WIKICORE_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h],
        )
