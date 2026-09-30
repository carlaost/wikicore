"""Keyword search over the in-memory vault. No index, no embeddings: fine for thousands of notes."""

from __future__ import annotations

import re
import unicodedata

from wikicore.vault import Note, Vault, links_in, slugify

WORD = re.compile(r"\w+")


def terms(text: str) -> list[str]:
    t = unicodedata.normalize("NFKD", text).casefold()
    t = "".join(c for c in t if not unicodedata.combining(c))
    return [w for w in WORD.findall(t.replace("_", " ")) if len(w) > 1]


def _snippet(n: Note, qs: list[str]) -> str:
    for line in n.body.splitlines():
        if any(q in line.lower() for q in qs) and not line.startswith("#"):
            return line.strip()[:200]
    return n.summary()


def search(vault: Vault, query: str, type: str | None = None, project: str | None = None, limit: int = 10) -> list[dict]:
    qs = terms(query)
    if not qs:
        return []
    proj = slugify(project) if project else None
    hits = []
    for n in vault.notes.values():
        if type and n.type != type:
            continue
        if proj and n.slug != proj and proj not in links_in(n.meta.get("projects")):
            continue
        title = set(terms(f"{n.title} {n.slug}"))
        alias = set(terms(" ".join(n.aliases)))
        meta = set(terms(" ".join(str(v) for k, v in n.meta.items() if k not in ("title", "aliases"))))
        body = terms(n.body)
        words = title | alias | meta | set(body)
        score = matched = 0
        for q in qs:
            s = 5 * (q in title) + 4 * (q in alias) + 2 * (q in meta) + min(body.count(q), 5)
            if not s and any(w.startswith(q) for w in words):
                s = 1
            matched += bool(s)
            score += s
        if not matched:
            continue
        if matched == len(qs):
            score *= 2
        hits.append({"slug": n.slug, "type": n.type, "title": n.title, "score": score, "snippet": _snippet(n, qs)})
    hits.sort(key=lambda h: (-h["score"], h["title"].lower()))
    return hits[:limit]


def mentions(vault: Vault, text: str, limit: int = 15) -> list[dict]:
    """Existing pages whose title or alias appears in `text` (names of 2+ characters)."""
    hay = f" {' '.join(terms(text))} "
    out = []
    for n in vault.notes.values():
        if n.type == "source":
            continue
        names = {" ".join(terms(x)) for x in (n.title, n.slug, *n.aliases)}
        if any(len(name) >= 2 and f" {name} " in hay for name in names):
            out.append({"slug": n.slug, "type": n.type, "title": n.title})
    return out[:limit]
