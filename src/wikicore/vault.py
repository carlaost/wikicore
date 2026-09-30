"""The vault: a folder of Markdown notes with YAML frontmatter. Folder = type (see _schema/)."""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

import frontmatter
import yaml

from wikicore.schema import TypeSchema, load_schemas

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
SERVER_OWNED = ("type", "created", "updated", "added_by")


class VaultError(Exception):
    """A problem the caller can fix. The message is shown to the assistant as-is."""


def slugify(text: str) -> str:
    return _ascii_slug(text)[:80].rstrip("-") or "untitled"


def _ascii_slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def check_slug(slug: str) -> str:
    if not SLUG_RE.fullmatch(slug or ""):
        raise VaultError(f"`{slug}` is not a valid slug (lowercase letters, digits and dashes)")
    return slug


def norm_name(text: str) -> str:
    base = _ascii_slug(text)
    if base:
        return slugify(text).replace("-", " ")
    return " ".join(str(text).casefold().split())  # no ASCII letters/digits: keep the original so names stay distinct


def links_in(value: Any) -> list[str]:
    if isinstance(value, str):
        return [slugify(m) for m in LINK_RE.findall(value)]
    if isinstance(value, dict):
        return [x for v in value.values() for x in links_in(v)]
    if isinstance(value, list):
        return [x for v in value for x in links_in(v)]
    return []


def _plain(v: Any) -> Any:
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    return v


@dataclass
class Note:
    slug: str
    type: str
    meta: dict[str, Any]
    body: str
    warnings: list[str] = field(default_factory=list)
    broken: bool = False   # frontmatter unreadable: served as raw text, must never be rewritten

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.slug)

    @property
    def aliases(self) -> list[str]:
        a = self.meta.get("aliases") or []
        return [str(x) for x in (a if isinstance(a, list) else [a])]

    def names(self) -> set[str]:
        return {norm_name(self.title), norm_name(self.slug), *(norm_name(a) for a in self.aliases)}

    def links(self) -> set[str]:
        return set(links_in(self.body)) | set(links_in(self.meta))

    def summary(self) -> str:
        for line in self.body.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line[:160]
        return ""


def _split_verbatim(raw: str) -> tuple[dict[str, Any], str] | None:
    """(meta, body) for a file we wrote ourselves: `---\\n<yaml>\\n---\\n<body>`; None if it does not look like one."""
    if not raw.startswith("---\n"):
        return None
    end = raw.find("\n---\n", 3)
    if end < 0:
        return None
    meta = yaml.safe_load(raw[4:end + 1]) or {}
    return (_plain(meta), raw[end + 5:]) if isinstance(meta, dict) else None


class Vault:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.load()

    def load(self) -> None:
        """Rebuild everything locally, then swap each attribute in with one assignment (readers run in other threads)."""
        schemas, problems = load_schemas(self.root)
        notes: dict[str, Note] = {}
        for s in schemas.values():
            folder = self.root / s.folder
            if not folder.is_dir():
                continue
            for p in sorted(folder.glob("*.md")):
                note = self._parse(p, s)
                if note.slug in notes:
                    problems.append(f"duplicate slug `{note.slug}` in {s.folder}/ and {notes[note.slug].type}")
                    continue
                notes[note.slug] = note
        backlinks = self._index_links(notes)
        self.schemas, self.problems, self.notes, self.backlinks = schemas, problems, notes, backlinks

    def _parse(self, path: Path, schema: TypeSchema) -> Note:
        with path.open(encoding="utf-8", errors="replace", newline="") as f:
            raw = f.read()
        broken = False
        try:
            verbatim = _split_verbatim(raw) if schema.name == "source" else None
            if verbatim:
                meta, body, warnings = verbatim[0], verbatim[1], []
            else:
                post = frontmatter.loads(raw)
                meta, body, warnings = dict(post.metadata), post.content, []
        except Exception as e:  # noqa: BLE001  a hand-edited page must never take the wiki down
            meta, body, warnings, broken = {}, raw, [f"frontmatter unreadable: {e}"], True
        meta = _plain(meta)
        if not SLUG_RE.fullmatch(path.stem):
            warnings.append(f"file name `{path.name}` is not a slug; rename it to edit this page through the wiki")
        return Note(slug=path.stem, type=schema.name, meta=meta, body=body, warnings=warnings + schema.validate(meta),
                    broken=broken)

    @staticmethod
    def _index_links(notes: dict[str, Note]) -> dict[str, set[str]]:
        backlinks: dict[str, set[str]] = {s: set() for s in notes}
        for n in notes.values():
            for t in n.links():
                if t in backlinks and t != n.slug:
                    backlinks[t].add(n.slug)
        return backlinks

    def get(self, slug: str) -> Note | None:
        return self.notes.get(slug)

    def suggest(self, text: str, n: int = 5) -> list[str]:
        keys = {s: s for s in self.notes} | {note.title.lower(): note.slug for note in self.notes.values()}
        return list(dict.fromkeys(keys[k] for k in difflib.get_close_matches(text.lower(), list(keys), n=n, cutoff=0.5)))

    def find_by_name(self, name: str, type: str | None = None) -> Note | None:
        key = norm_name(name)
        for n in self.notes.values():
            if (type is None or n.type == type) and key in n.names():
                return n
        return None

    def path_for(self, type: str, slug: str) -> Path:
        s = self.schemas.get(type)
        if not s:
            raise VaultError(f"unknown type `{type}`; types are: {', '.join(sorted(self.schemas))}")
        return self.root / s.folder / f"{check_slug(slug)}.md"

    def write(self, note: Note) -> Path:
        path = self.path_for(note.type, note.slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        head = "---\n" + yaml.safe_dump(note.meta, sort_keys=False, allow_unicode=True) + "---\n"
        # Sources are verbatim: the body is exactly what follows the closing `---` line. Other pages are normalised.
        text = head + note.body if note.type == "source" else head + "\n" + note.body.strip() + "\n"
        tmp = path.with_suffix(".md.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            f.write(text)
        tmp.replace(path)
        # Publish a private copy carrying the warnings; the caller's Note is never touched (readers hold the stored one).
        stored = replace(note, meta=dict(note.meta), warnings=self.schemas[note.type].validate(note.meta))
        notes = {**self.notes, note.slug: stored}
        backlinks = self._index_links(notes)
        self.notes, self.backlinks = notes, backlinks
        return path

    def rel(self, path: Path) -> str:
        return str(Path(path).relative_to(self.root))

    def dangling(self) -> list[tuple[str, str]]:
        return sorted((n.slug, t) for n in self.notes.values() for t in n.links() if t not in self.notes)


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, text) for every real heading; `#` lines inside fenced code are not headings."""
    out, fence = [], None
    for i, line in enumerate(lines):
        f = FENCE_RE.match(line)
        if f:
            fence = None if fence == f.group(1) else (fence or f.group(1))
            continue
        m = None if fence else HEADING_RE.match(line)
        if m:
            out.append((i, len(m.group(1)), m.group(2)))
    return out


def _find_heading(lines: list[str], section: str) -> tuple[int, int] | None:
    heads = _headings(lines)
    for k, (i, level, text) in enumerate(heads):
        if text.lower() == section.lower():
            end = next((j for j, lv, _ in heads[k + 1:] if lv <= level), len(lines))
            return i, end
    return None


def get_section(body: str, section: str) -> str | None:
    lines = body.splitlines()
    found = _find_heading(lines, section)
    return "\n".join(lines[found[0] + 1:found[1]]).strip() if found else None


def edit_section(body: str, section: str, content: str, mode: str) -> str:
    lines = body.splitlines()
    found = _find_heading(lines, section)
    if found is None:
        return (body.rstrip() + f"\n\n## {section}\n\n{content.strip()}\n").lstrip()
    start, end = found
    existing = "\n".join(lines[start + 1:end]).strip()
    new = content.strip() if mode == "replace" or not existing else existing + "\n" + content.strip()
    return "\n".join(lines[:start + 1] + ["", new, ""] + lines[end:]).strip() + "\n"


def merge_frontmatter(meta: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    out = dict(meta)
    for k, v in updates.items():
        if k in SERVER_OWNED:
            continue
        if v is None:
            out.pop(k, None)
        elif isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = list(dict.fromkeys([*out[k], *v]))
        else:
            out[k] = v
    return out


def render_index(vault: Vault) -> str:
    parts = ["# Index", ""]
    for type_name in sorted(vault.schemas):
        notes = sorted((n for n in vault.notes.values() if n.type == type_name), key=lambda n: n.title.lower())
        if not notes:
            continue
        parts += [f"## {type_name}", ""]
        parts += [f"- [[{n.slug}]] {n.title}" + (f": {n.summary()}" if n.summary() else "") for n in notes]
        parts.append("")
    return "\n".join(parts)
