"""Create a new vault from the packaged defaults."""

from __future__ import annotations

import shutil
from importlib.resources import as_file, files
from pathlib import Path

from wikicore.schema import load_schemas


def init_vault(root: Path) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with as_file(files("wikicore") / "defaults") as src:
        for f in sorted(src.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                dest = root / f.relative_to(src)
                if not dest.exists():   # never overwrite a customised file
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(f, dest)
    schemas, _ = load_schemas(root)
    for s in schemas.values():
        (root / s.folder).mkdir(exist_ok=True)
        (root / s.folder / ".gitkeep").touch()
    for name, text in (("index.md", "# Index\n"), ("log.md", "# Log\n\n")):
        if not (root / name).exists():
            (root / name).write_text(text, encoding="utf-8")
    return root
