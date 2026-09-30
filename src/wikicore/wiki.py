"""Every wiki operation. MCP tools and web routes are thin wrappers over this class."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import threading
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from pypdf import PdfReader

from wikicore.bootstrap import guide_for
from wikicore.config import Config
from wikicore.custom import load_custom
from wikicore.gitops import Git
from wikicore.repos import Repos
from wikicore.schema import SCHEMA_DIR, TypeSchema
from wikicore.search import mentions, search, terms
from wikicore.vault import (SERVER_OWNED, Note, Vault, VaultError, check_slug, edit_section, links_in,
                            get_section, merge_frontmatter, render_index, slugify)

log = logging.getLogger(__name__)

FILES_DIR = "_files"
MAX_UPLOAD = 25 * 1024 * 1024
TEXT_SUFFIXES = (".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm")
BOOTSTRAP_FILE = "_meta/bootstrap.yaml"
LOG_FILE = "log.md"
INDEX_FILE = "index.md"
COMMON_FIELDS = {
    "title": "page title (required)",
    "aliases": "other names, spellings, old project names; used for dedupe and search",
    "projects": 'list of "[[project-slug]]" this page belongs to',
    "sources": 'list of "[[source-id]]" this page was filed from',
    "created / updated / added_by / type": "set by the server",
}


def _clean(text: str) -> str:
    """One line, single spaces: keeps log.md and commit subjects from being forged with newlines."""
    return " ".join(str(text).split())


def _broken_msg(n: Note) -> str:
    first = (n.warnings[0] if n.warnings else "").replace("frontmatter unreadable: ", "").splitlines()[:1]
    return (f"`{n.slug}` has unreadable frontmatter ({first[0] if first else 'invalid YAML'}); "
            "the owner must fix the page's frontmatter by hand, then retry")


def extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        try:
            return "\n\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages).strip()
        except Exception:  # noqa: BLE001  an unreadable PDF is still worth keeping as a file
            return ""
    if name.endswith(TEXT_SUFFIXES):
        return data.decode("utf-8", errors="replace")
    return ""


def client_filename(filename: str) -> str:
    """The last path component of a client-supplied name, whichever separator it used."""
    return re.split(r"[\\/]", str(filename))[-1]


def safe_filename(filename: str) -> str:
    """Slugified stem plus a short alphanumeric extension; never contains a path separator."""
    base = client_filename(filename)
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    ext = re.sub(r"[^a-z0-9]", "", ext.lower())[:10]
    return slugify(stem) + (f".{ext}" if ext else "")


class UploadTooLarge(VaultError):
    pass


class Wiki:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = cfg.vault
        self.vault = Vault(cfg.vault)
        self.lock = threading.RLock()
        self.git = Git(cfg.vault, push=cfg.git_push)
        self.repos = Repos(cfg.vault, cfg.state_dir / "repos", cfg.github_token, cfg.repo_ttl)
        self.on_schema_change: Callable[[], None] | None = None

    # ── read ────────────────────────────────────────────────────────────────
    def _brief(self, slug: str) -> dict[str, Any]:
        n = self.vault.get(slug)
        return {"slug": slug, "type": n.type, "title": n.title} if n else {"slug": slug, "missing": True}

    def search(self, query: str, type: str | None = None, project: str | None = None, limit: int = 10) -> dict[str, Any]:
        hits = search(self.vault, query, type, project, limit)
        out: dict[str, Any] = {"results": hits}
        if not hits:
            out["hint"] = "Nothing matched. Retry with fewer words or synonyms; `schema` lists the types."
        return out

    def get(self, slug: str) -> dict[str, Any]:
        n = self.vault.get(slug)
        if not n:
            return {"found": False, "did_you_mean": self.vault.suggest(slug)}
        return {"found": True, "slug": n.slug, "type": n.type, "frontmatter": n.meta, "body": n.body,
                "backlinks": sorted(self.vault.backlinks.get(slug, ())), "warnings": n.warnings}

    def related(self, slug: str) -> dict[str, Any]:
        n = self.vault.get(slug)
        if not n:
            return {"found": False, "did_you_mean": self.vault.suggest(slug)}
        return {"slug": slug, "links_to": [self._brief(s) for s in sorted(n.links() - {slug})],
                "linked_from": [self._brief(s) for s in sorted(self.vault.backlinks.get(slug, ()))]}

    def list_notes(self, type: str, filters: dict[str, Any] | None = None, limit: int = 100) -> dict[str, Any]:
        def ok(meta: dict[str, Any]) -> bool:
            for k, want in (filters or {}).items():
                have = meta.get(k)
                if isinstance(have, list):
                    if want not in have and not (set(links_in(want)) & set(links_in(have))):
                        return False
                elif have != want:
                    return False
            return True
        notes = sorted((n for n in self.vault.notes.values() if n.type == type and ok(n.meta)), key=lambda n: n.title.lower())
        return {"type": type, "count": len(notes),
                "notes": [{"slug": n.slug, "title": n.title, "summary": n.summary()} for n in notes[:limit]]}

    def recent(self, n: int = 20, type: str | None = None) -> dict[str, Any]:
        path = self.root / LOG_FILE
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        out = []
        for line in reversed(lines):
            parts = line.removeprefix("- ").split(" | ")
            if len(parts) != 4:
                continue
            entry = dict(zip(("when", "client", "action", "summary"), parts))
            slug = entry["summary"].split(" ")[0]
            if type and (self.vault.get(slug) is None or self.vault.get(slug).type != type):
                continue
            out.append(entry)
            if len(out) >= n:
                break
        return {"changes": out}

    def schema(self) -> dict[str, Any]:
        return {"types": {s.name: {"description": s.description, "folder": s.folder, "fields": s.fields,
                                   "required": s.required, "sections": s.sections} for s in self.vault.schemas.values()},
                "common_fields": COMMON_FIELDS}

    def status(self) -> dict[str, Any]:
        # Read each attribute once: load() swaps them one by one, so tolerate a schemas/notes mismatch.
        schemas, notes, problems = self.vault.schemas, self.vault.notes, self.vault.problems
        pages: dict[str, int] = {t: 0 for t in schemas}
        for n in notes.values():
            pages[n.type] = pages.get(n.type, 0) + 1
        unfiled = [{"source_id": n.slug, "title": n.title, "kind": n.meta.get("kind")}
                   for n in notes.values() if n.type == "source" and not n.meta.get("filed")]
        out: dict[str, Any] = {"pages": pages, "unfiled_sources": unfiled,
                               "problems": problems + [f"{n.slug}: {w}" for n in notes.values() for w in n.warnings]}
        if self.repos.enabled:
            out["repos"] = self.repos.list()
            out["problems"] = out["problems"] + [f"repos: {m}" for m in self.repos.problems]
        runs, bad = self._bootstrap_runs()
        out["bootstrap"] = {r["client"]: r["date"] for r in runs}
        out["problems"] = out["problems"] + [f"bootstrap: {m}" for m in bad]
        return out

    # ── write plumbing ──────────────────────────────────────────────────────
    def _write(self, client: str, action: str, work: Callable[[], tuple[list[Path], str, dict[str, Any]]]) -> dict[str, Any]:
        """Pull, run `work` (which writes files and returns (paths, summary, result)), log, index, commit, push."""
        with self.lock:
            client, action = _clean(client), _clean(action)
            before = self.git.head()
            warnings = self._pull_warnings()
            if self.git.head() != before:
                self.vault.load()
            try:
                paths, summary, result = work()
            except Exception:
                self.vault.load()   # drop any half-applied in-memory changes
                raise
            if not paths:
                return result
            summary = _clean(summary)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            with (self.root / LOG_FILE).open("a", encoding="utf-8") as f:
                f.write(f"- {stamp} | {client} | {action} | {summary}\n")
            (self.root / INDEX_FILE).write_text(render_index(self.vault), encoding="utf-8")
            rels = [self.vault.rel(p) for p in paths] + [LOG_FILE, INDEX_FILE]
            if not self.git.commit(rels, f"{action}: {summary}", client) and self.git.commit_error:
                warnings.append(f"The change is saved in the vault files but git could not commit it: {self.git.commit_error}")
            elif not self.git.conflict:
                err = self.git.push()
                if err:
                    warnings.append(f"Saved on the server; pushing to the vault remote failed and will be retried: {err}")
            if warnings:
                result["sync_warning"] = " ".join(warnings)
            return result

    def _pull_warnings(self) -> list[str]:
        err = self.git.pull()
        if not err:
            return []
        if self.git.conflict:
            return ["The vault has diverged from the remote (a rebase conflict, aborted) and needs manual resolution; "
                    f"nothing is pushed until then: {err}"]
        return [f"Could not pull from the vault remote: {err}"]

    def sync(self) -> dict[str, Any]:
        """Periodic: pull changes made elsewhere and retry pending pushes.

        Returns {"changed": bool, "errors": [...]}; errors is empty when everything worked."""
        with self.lock:
            before = self.git.head()
            errors = self._pull_warnings()
            if not self.git.conflict:
                err = self.git.push()
                if err:
                    errors.append(f"Pushing to the vault remote failed: {err}")
            changed = self.git.head() != before
            if changed:
                self.vault.load()
            return {"changed": changed, "errors": errors}

    def _unique(self, base: str) -> str:
        slug, i = base, 2
        while slug in self.vault.notes:
            slug, i = f"{base}-{i}", i + 1
        return slug

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    # ── bootstrap ───────────────────────────────────────────────────────────
    def _bootstrap_runs(self) -> tuple[list[dict[str, Any]], list[str]]:
        """Valid run records plus problems. The file is hand-edited, so a bad file or entry is
        reported, never raised."""
        p = self.root / BOOTSTRAP_FILE
        if not p.exists():
            return [], []
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or []
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
            return [], [f"{BOOTSTRAP_FILE} could not be read: {' '.join(str(e).split())[:200]}"]
        if not isinstance(raw, list):
            return [], [f"{BOOTSTRAP_FILE} must be a list of runs"]
        runs: list[dict[str, Any]] = []
        problems: list[str] = []
        for i, r in enumerate(raw):
            if isinstance(r, dict) and isinstance(r.get("client"), str) and r.get("date") is not None:
                runs.append({**r, "date": str(r["date"])})
            else:
                problems.append(f"{BOOTSTRAP_FILE} entry {i} skipped: needs `client` and `date`")
        return runs, problems

    def bootstrap(self, client: str, as_client: str | None = None, done: bool = False, summary: str | None = None,
                  created: int | None = None, updated: int | None = None) -> dict[str, Any]:
        key, steps = guide_for(as_client or client)
        if not done:
            runs, _ = self._bootstrap_runs()
            return {"client": key, "steps": steps, "rules": load_custom(self.root).instructions,
                    "previous_runs": [r for r in runs if r.get("client") == key]}

        def work() -> tuple[list[Path], str, dict[str, Any]]:
            p = self.root / BOOTSTRAP_FILE
            if p.exists():
                try:
                    existing = yaml.safe_load(p.read_text(encoding="utf-8")) or []
                except (yaml.YAMLError, UnicodeDecodeError) as e:
                    raise VaultError(f"{BOOTSTRAP_FILE} is not valid YAML; fix or remove it before recording a run: "
                                     f"{' '.join(str(e).split())[:200]}") from e
                if not isinstance(existing, list):
                    raise VaultError(f"{BOOTSTRAP_FILE} must be a list of runs; fix or remove it before recording a run.")
            else:
                existing = []
            run: dict[str, Any] = {"client": key, "date": self._today(), "summary": _clean(summary or "")}
            if created is not None:
                run["created"] = created
            if updated is not None:
                run["updated"] = updated
            p.parent.mkdir(exist_ok=True)
            p.write_text(yaml.safe_dump(existing + [run], sort_keys=False, allow_unicode=True), encoding="utf-8")
            return [p], f"{key} done", {"recorded": True, "client": key}
        return self._write(client, "bootstrap", work)

    # ── sources ─────────────────────────────────────────────────────────────
    def _new_source(self, text: str, kind: str, client: str, title: str | None, url: str | None,
                    file: str | None) -> tuple[list[Path], str, dict[str, Any]]:
        title = (title or " ".join(text.split()[:8]) or "untitled").strip()
        slug = self._unique(slugify(f"{self._today()} {title}"))
        meta: dict[str, Any] = {"type": "source", "title": title, "kind": kind, "created": self._today(),
                                "updated": self._today(), "added_by": client, "filed": False}
        if url:
            meta["url"] = url
        if file:
            meta["file"] = file
        path = self.vault.write(Note(slug, "source", meta, text))
        types = {s.name: s.description for s in self.vault.schemas.values() if s.name != "source"}
        return [path], slug, {
            "saved": True, "source_id": slug, "chars": len(text), "mentions": mentions(self.vault, f"{title} {text}"),
            "types": types,
            "next": "File what this contains: `update` the existing pages in `mentions` and `file` new ones, passing this "
                    "source_id to both, then tell the user in one line what you filed.",
        }

    def save_source(self, text: str, kind: str, client: str, title: str | None = None, url: str | None = None) -> dict[str, Any]:
        if not text.strip():
            raise VaultError("nothing to save: `text` is empty")
        return self._write(client, "save_source", lambda: self._new_source(text, kind, client, title, url, None))

    def save_upload(self, filename: str, data: bytes, note: str, client: str) -> dict[str, Any]:
        if len(data) > MAX_UPLOAD:
            raise UploadTooLarge("file is larger than 25 MB")
        safe = safe_filename(filename)
        shown = client_filename(filename) or "upload"
        text = extract_text(shown, data)
        if text:
            content = text
        elif shown.lower().endswith(".pdf"):
            content = f"(text extraction failed or found no text in {shown}; the original file is stored)"
        else:
            content = f"(no text could be extracted from {shown}; the original file is stored)"
        body = (note.strip() + "\n\n---\n\n" if note.strip() else "") + content
        title = Path(shown).stem or "upload"

        def work() -> tuple[list[Path], str, dict[str, Any]]:
            base = slugify(f"{self._today()} {title}")
            name, i = base, 2
            while (self.root / FILES_DIR / name).exists():
                name, i = f"{base}-{i}", i + 1
            folder = self.root / FILES_DIR / name
            folder.mkdir(parents=True)
            target = folder / safe
            target.write_bytes(data)
            rel = self.vault.rel(target)
            paths, slug, result = self._new_source(body, "upload", client, title, None, rel)
            return [target, *paths], slug, result | {"file": rel}
        return self._write(client, "upload", work)

    # ── pages ───────────────────────────────────────────────────────────────
    def _similar(self, title: str, type: str) -> list[str]:
        mine = set(terms(title))
        out = []
        for n in self.vault.notes.values():
            if n.type != type:
                continue
            for name in n.names():
                theirs = set(name.split())
                if mine and theirs and (mine <= theirs or theirs <= mine):
                    out.append(n.slug)
                    break
        return out

    def _source(self, source_id: str) -> Note:
        src = self.vault.get(source_id)
        if not src or src.type != "source":
            raise VaultError(f"no source `{source_id}`")
        if src.broken:
            raise VaultError(_broken_msg(src))
        return src

    def _mark_filed(self, src: Note, into: str) -> Path:
        smeta = {**src.meta, "filed": True, "updated": self._today(),
                 "filed_into": list(dict.fromkeys([*(src.meta.get("filed_into") or []), f"[[{into}]]"]))}
        return self.vault.write(Note(src.slug, src.type, smeta, src.body))

    def file(self, type: str, title: str, client: str, frontmatter: dict[str, Any] | None = None,
             body: str = "", source_id: str | None = None) -> dict[str, Any]:
        fm = dict(frontmatter or {})
        if type == "source":
            raise VaultError("sources are created with `save_source`, not `file`")
        if not title.strip():
            raise VaultError("`title` is required")

        def work() -> tuple[list[Path], str, dict[str, Any]]:
            schema = self.vault.schemas.get(type)
            if not schema:
                raise VaultError(f"unknown type `{type}`; types are: {', '.join(sorted(self.vault.schemas))}; "
                                 "add one with `add_type`")
            aliases = fm.get("aliases") if isinstance(fm.get("aliases"), list) else []
            existing = next((n for name in [title, *aliases] if (n := self.vault.find_by_name(str(name), type))), None)
            slug = slugify(title)
            if slug == "untitled" and title.strip():   # no ASCII letters or digits to build a slug from
                slug = f"{type}-{hashlib.sha1(title.strip().encode('utf-8')).hexdigest()[:6]}"
            if not existing and slug in self.vault.notes:
                other = self.vault.notes[slug]
                raise VaultError(f"`{slug}` is already a {other.type} page; give this {type} a more specific title")
            if existing:
                return [], "", {"created": False, "existing": self.get(existing.slug),
                                "hint": f"`{existing.slug}` already exists; add to it with `update`."}
            similar = self._similar(title, type)
            meta: dict[str, Any] = {"type": type, "title": title.strip()}
            meta |= {k: v for k, v in fm.items() if k not in (*SERVER_OWNED, "title")}
            meta |= {"created": self._today(), "updated": self._today(), "added_by": client}
            paths = []
            if source_id:
                src = self._source(source_id)
                meta["sources"] = list(dict.fromkeys([*(meta.get("sources") or []), f"[[{source_id}]]"]))
                paths.append(self._mark_filed(src, slug))
            note = Note(slug, type, meta, body.strip() or schema.skeleton())
            paths.insert(0, self.vault.write(note))
            return paths, slug, {"created": True, "slug": slug, "type": type, "warnings": self.vault.notes[slug].warnings, "similar": similar}

        return self._write(client, "file", work)

    def update(self, slug: str, client: str, section: str | None = None, content: str | None = None,
               mode: str = "append", frontmatter: dict[str, Any] | None = None,
               source_id: str | None = None) -> dict[str, Any]:
        if mode not in ("append", "replace"):
            raise VaultError("`mode` is append or replace")
        if content is None and not frontmatter:
            raise VaultError("nothing to change: pass `content` and/or `frontmatter`")

        def work() -> tuple[list[Path], str, dict[str, Any]]:
            n = self.vault.get(slug)
            if not n:
                hint = self.vault.suggest(slug)
                raise VaultError(f"no page `{slug}`" + (f"; did you mean {', '.join(hint)}?" if hint else ""))
            if n.broken:
                raise VaultError(_broken_msg(n))
            if n.type == "source" and (content is not None or frontmatter):
                raise VaultError("sources are verbatim and are not edited; file what they contain into other pages")
            src = self._source(source_id) if source_id else None
            if src and src.slug == n.slug:
                raise VaultError("a source cannot be filed into itself")
            fm = dict(frontmatter or {})
            if src:
                fm["sources"] = [*(fm["sources"] if isinstance(fm.get("sources"), list) else []), f"[[{src.slug}]]"]
            meta = merge_frontmatter(n.meta, fm)
            meta["updated"] = self._today()
            text = n.body
            if content is not None:
                if section:
                    text = edit_section(text, section, content, mode)
                else:
                    text = content if mode == "replace" else text.rstrip() + "\n\n" + content.strip()
            note = Note(n.slug, n.type, meta, text)
            paths = [self.vault.write(note)]
            if src:
                paths.append(self._mark_filed(src, n.slug))
            return paths, n.slug + (f" §{section}" if section else ""), {"updated": True, "slug": n.slug, "warnings": self.vault.notes[n.slug].warnings}

        return self._write(client, "update", work)

    def add_type(self, name: str, description: str, client: str, fields: dict[str, str] | None = None,
                 sections: list[str] | None = None, required: list[str] | None = None, folder: str | None = None) -> dict[str, Any]:
        type_name = check_slug(slugify(name))
        if not description.strip():
            raise VaultError("`description` is required: say when this type should be used")

        def work() -> tuple[list[Path], str, dict[str, Any]]:
            if type_name in self.vault.schemas:
                raise VaultError(f"type `{type_name}` exists")
            folder_name = check_slug(slugify(folder or type_name))
            owner = next((t.name for t in self.vault.schemas.values() if t.folder == folder_name), None)
            if owner:
                raise VaultError(f"folder `{folder_name}` already belongs to type `{owner}`; pick another folder")
            s = TypeSchema(name=type_name, folder=folder_name, description=description.strip(),
                           fields=dict(fields or {}), required=list(required or []), sections=list(sections or []))
            p = self.root / SCHEMA_DIR / f"{type_name}.yaml"
            p.write_text(s.to_yaml(), encoding="utf-8")
            keep = self.root / s.folder / ".gitkeep"
            keep.parent.mkdir(exist_ok=True)
            keep.touch()
            self.vault.load()
            return [p, keep], type_name, {"added": True, "type": type_name, "folder": s.folder}

        result = self._write(client, "add_type", work)
        if self.on_schema_change:
            try:
                self.on_schema_change()
            except Exception:  # noqa: BLE001  the type is already committed; never turn that into an error
                log.exception("on_schema_change failed after add_type")
        return result

    # ── repos ───────────────────────────────────────────────────────────────
    def repo_list(self) -> dict[str, Any]:
        out: dict[str, Any] = {"repos": self.repos.list()}
        if self.repos.problems:
            out["problems"] = list(self.repos.problems)
        return out

    def register_repo(self, url: str, client: str, project: str | None = None, name: str | None = None) -> dict[str, Any]:
        def work() -> tuple[list[Path], str, dict[str, Any]]:
            path, repo_name = self.repos.register(url, project, name)
            cloned = self.repos.ensure(repo_name)
            return [path], repo_name, {"registered": True, "name": repo_name, "cloned": cloned,
                                       "error": self.repos.errors.get(repo_name)}
        return self._write(client, "register_repo", work)

    def repo_status(self, name: str) -> dict[str, Any]:
        s = self.repos.status(name)
        page = self.vault.get(s["project"]) if s.get("project") else None
        if page:
            s["project"] = {"slug": page.slug, "status": get_section(page.body, "Status"), "open": get_section(page.body, "Open")}
        return s

    def repo_read(self, name: str, path: str) -> dict[str, Any]:
        return self.repos.read(name, path)

    def repo_log(self, name: str, n: int = 20, path: str | None = None) -> dict[str, Any]:
        return self.repos.log(name, n, path)

    def repo_tree(self, name: str, path: str = "") -> dict[str, Any]:
        return self.repos.tree(name, path)
