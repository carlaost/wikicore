import subprocess
from pathlib import Path

import pytest

from wikicore.init_vault import init_vault


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    d = init_vault(tmp_path / "vault")
    git(d, "init", "-q")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "init")
    return d


@pytest.fixture
def write_page(vault_dir: Path):
    """Write a page straight to disk, the way a human in Obsidian would."""
    def _write(folder: str, slug: str, meta: str, body: str = "") -> Path:
        p = vault_dir / folder / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\n{meta.strip()}\n---\n\n{body}\n", encoding="utf-8")
        return p
    return _write
