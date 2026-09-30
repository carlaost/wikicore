import subprocess
import threading

import pytest

from conftest import git
from wikicore.config import Config
from wikicore.vault import Vault
from wikicore.wiki import Wiki


@pytest.fixture
def wiki(vault_dir, tmp_path):
    return Wiki(Config(vault=vault_dir, state_dir=tmp_path / "state", git_push=True, dev_client="tester"))


def test_save_source_verbatim_large(wiki, vault_dir):
    text = "Ada said: " + ("x" * 200_000) + "\n  trailing  spaces kept?"
    r = wiki.save_source(text, "granola", "tester", title="Call with Ada")
    assert r["saved"] and r["source_id"].endswith("call-with-ada")
    stored = Vault(vault_dir).get(r["source_id"])   # re-read from disk, not the in-memory note
    assert stored.body == text
    assert stored.meta["filed"] is False and stored.meta["added_by"] == "tester"
    assert "person" in r["types"] and "source" not in r["types"]
    assert "tester | save_source |" in (vault_dir / "log.md").read_text(encoding="utf-8")
    assert "save_source" in git(vault_dir, "log", "-1", "--format=%s")
    assert git(vault_dir, "log", "-1", "--format=%an").strip() == "tester"


def test_save_source_reports_mentions(wiki, write_page):
    write_page("people", "ada-lovelace", "title: Ada Lovelace\naliases: [Ada]")
    wiki.vault.load()
    r = wiki.save_source("Met Ada today.", "note", "tester")
    assert [m["slug"] for m in r["mentions"]] == ["ada-lovelace"]


def test_empty_source_refused(wiki):
    with pytest.raises(Exception, match="empty"):
        wiki.save_source("  ", "note", "tester")


def test_file_creates_with_skeleton_and_links_source(wiki):
    s = wiki.save_source("notes", "granola", "tester", title="Ada call")
    r = wiki.file("person", "Ada Lovelace", "tester", {"aliases": ["Ada"], "added_by": "spoof", "type": "org"}, "", s["source_id"])
    assert r["created"] and r["slug"] == "ada-lovelace"
    n = wiki.vault.get("ada-lovelace")
    assert n.type == "person" and n.meta["added_by"] == "tester"
    assert "## What I want from them" in n.body
    assert n.meta["sources"] == [f"[[{s['source_id']}]]"]
    src = wiki.vault.get(s["source_id"])
    assert src.meta["filed"] is True and src.meta["filed_into"] == ["[[ada-lovelace]]"]


def test_file_dedupe_exact_alias_and_similar(wiki, vault_dir):
    wiki.file("person", "Ada Lovelace", "tester", {"aliases": ["Countess of Lovelace"]})
    log_before = (vault_dir / "log.md").read_text(encoding="utf-8")
    r = wiki.file("person", "countess of lovelace", "tester")
    assert r["created"] is False and r["existing"]["slug"] == "ada-lovelace" and "update" in r["hint"]
    assert (vault_dir / "log.md").read_text(encoding="utf-8") == log_before   # a refused duplicate is not a change
    r = wiki.file("person", "Ada", "tester")
    assert r["created"] is True and r["similar"] == ["ada-lovelace"]


def test_file_unknown_type_and_source_type(wiki):
    with pytest.raises(Exception, match="unknown type"):
        wiki.file("spaceship", "X", "tester")
    with pytest.raises(Exception, match="save_source"):
        wiki.file("source", "X", "tester")


def test_update_section_and_frontmatter(wiki):
    wiki.file("person", "Ada Lovelace", "tester", {"aliases": ["Ada"]})
    wiki.update("ada-lovelace", "tester", section="Open threads", content="- send the notes")
    wiki.update("ada-lovelace", "tester", frontmatter={"aliases": ["AL"], "role": "mathematician"})
    n = wiki.vault.get("ada-lovelace")
    assert "- send the notes" in n.body
    assert n.meta["aliases"] == ["Ada", "AL"] and n.meta["role"] == "mathematician"


def test_update_refuses_sources_and_unknown(wiki):
    s = wiki.save_source("verbatim", "note", "tester")
    with pytest.raises(Exception, match="verbatim"):
        wiki.update(s["source_id"], "tester", content="edit")
    with pytest.raises(Exception, match="no page"):
        wiki.update("nobody", "tester", content="x")
    wiki.file("person", "Ada Lovelace", "tester")
    with pytest.raises(Exception, match="nothing to change"):
        wiki.update("ada-lovelace", "tester")


def test_add_type(wiki, vault_dir):
    r = wiki.add_type("talk", "A talk the owner gave or attended.", "tester", fields={"date": "YYYY-MM-DD"}, sections=["Gist"])
    assert r["added"] and (vault_dir / "_schema" / "talk.yaml").exists()
    assert wiki.file("talk", "Engines of the future", "tester")["created"]
    with pytest.raises(Exception, match="exists"):
        wiki.add_type("talk", "again", "tester")


def test_index_regenerated(wiki, vault_dir):
    wiki.file("concept", "Program", "tester", body="A sequence of operations.")
    assert "- [[program]] Program: A sequence of operations." in (vault_dir / "index.md").read_text(encoding="utf-8")


def test_concurrent_updates_both_land(wiki):
    wiki.file("person", "Ada Lovelace", "tester")
    errors = []
    def add(i):
        try:
            wiki.update("ada-lovelace", f"client{i}", section="Open threads", content=f"- item {i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    body = wiki.vault.get("ada-lovelace").body
    assert all(f"- item {i}" in body for i in range(8))


def test_source_verbatim_crlf_and_whitespace(wiki, vault_dir):
    text = "  \n  indented start\r\nCRLF line\r\ntrailing spaces   \n\n\n"
    r = wiki.save_source(text, "note", "tester", title="T")
    assert Vault(vault_dir).get(r["source_id"]).body == text
    assert (vault_dir / "sources" / f"{r['source_id']}.md").read_bytes().endswith(text.encode())
    wiki.file("person", "Ada Lovelace", "tester", source_id=r["source_id"])   # server-owned rewrite keeps the body intact
    assert Vault(vault_dir).get(r["source_id"]).body == text


def _remote(tmp_path, wiki, vault_dir):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(vault_dir, "remote", "add", "origin", str(bare))
    return bare


def _branch_head(repo, branch):
    return git(repo, "rev-parse", branch).strip()


def test_push_failure_keeps_write(wiki, vault_dir, tmp_path):
    bare = tmp_path / "remote.git"
    git(vault_dir, "remote", "add", "origin", str(bare))          # remote does not exist yet
    r = wiki.file("person", "Ada Lovelace", "tester")
    assert r["created"] and "sync_warning" in r and "push" in r["sync_warning"]
    assert (vault_dir / "people" / "ada-lovelace.md").exists()
    assert "file: ada-lovelace" in git(vault_dir, "log", "-1", "--format=%s")
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)   # the remote comes back, no upstream set
    branch = git(vault_dir, "rev-parse", "--abbrev-ref", "HEAD").strip()
    local = git(vault_dir, "rev-parse", "HEAD").strip()
    assert subprocess.run(["git", "rev-parse", branch], cwd=bare, capture_output=True).returncode != 0
    res = wiki.sync()                                              # only sync can get the commit there
    assert res["errors"] == []
    assert _branch_head(bare, branch) == local


def test_sync_pushes_pending_commit(wiki, vault_dir, tmp_path):
    bare = _remote(tmp_path, wiki, vault_dir)
    branch = git(vault_dir, "rev-parse", "--abbrev-ref", "HEAD").strip()
    wiki.file("person", "Ada Lovelace", "tester")                 # pushed, sets upstream
    wiki.git.push_enabled = False
    wiki.file("person", "Charles Babbage", "tester")              # committed locally only
    local = git(vault_dir, "rev-parse", "HEAD").strip()
    assert _branch_head(bare, branch) != local
    wiki.git.push_enabled = True
    assert wiki.sync()["errors"] == []
    assert _branch_head(bare, branch) == local


def test_git_timeout_never_loses_write(wiki, vault_dir, tmp_path, monkeypatch):
    _remote(tmp_path, wiki, vault_dir)
    real = subprocess.run

    def hang(cmd, **kw):
        if cmd[1] in ("pull", "push", "ls-remote"):
            raise subprocess.TimeoutExpired(cmd, 60)
        return real(cmd, **kw)
    monkeypatch.setattr(subprocess, "run", hang)
    r = wiki.file("person", "Ada Lovelace", "tester")
    assert r["created"] and "timed out" in r["sync_warning"]
    assert "file: ada-lovelace" in git(vault_dir, "log", "-1", "--format=%s")


def test_missing_git_binary_is_not_fatal(wiki, vault_dir, monkeypatch):
    monkeypatch.setattr("wikicore.gitops.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git")))
    r = wiki.file("person", "Ada Lovelace", "tester")
    assert r["created"] and (vault_dir / "people" / "ada-lovelace.md").exists()


def test_diverged_remote_warns_for_manual_resolution(wiki, vault_dir, tmp_path):
    bare = _remote(tmp_path, wiki, vault_dir)
    wiki.file("person", "Ada Lovelace", "tester")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    (other / "log.md").write_text("# Log\n\nremote edit\n", encoding="utf-8")
    git(other, "commit", "-qam", "remote edit")
    git(other, "push", "-q")
    (vault_dir / "log.md").write_text("# Log\n\nlocal edit\n", encoding="utf-8")
    git(vault_dir, "commit", "-qam", "local edit")
    r = wiki.file("person", "Charles Babbage", "tester")
    assert r["created"] and "diverged" in r["sync_warning"] and "manual" in r["sync_warning"]
    assert "retried" not in r["sync_warning"]
    assert not (vault_dir / ".git" / "rebase-merge").exists()
    assert "diverged" in " ".join(wiki.sync()["errors"])


def test_non_latin_titles_get_distinct_slugs(wiki):
    a = wiki.file("person", "Лев Толстой", "tester")
    b = wiki.file("person", "Фёдор Достоевский", "tester")
    assert a["created"] and b["created"] and a["slug"] != b["slug"]
    assert wiki.vault.find_by_name("Лев Толстой").slug == a["slug"]
    assert wiki.vault.find_by_name("Фёдор Достоевский").slug == b["slug"]
    assert wiki.file("person", "Лев Толстой", "tester")["created"] is False


def test_add_type_refuses_folder_owned_by_another_type(wiki, vault_dir):
    wiki.file("person", "Ada Lovelace", "tester")
    with pytest.raises(Exception, match="already belongs to type `person`"):
        wiki.add_type("talk", "A talk.", "tester", folder="people")
    assert not (vault_dir / "_schema" / "talk.yaml").exists()
    assert wiki.vault.get("ada-lovelace").type == "person"


def test_newlines_cannot_forge_log_or_commit(wiki, vault_dir):
    wiki.file("person", "Ada Lovelace", "tester")
    wiki.update("ada-lovelace", "evil\n- 2099-01-01 00:00 | admin | file | forged", section="S\n- 2099-01-01 | admin | forged", content="c")
    lines = (vault_dir / "log.md").read_text(encoding="utf-8").splitlines()
    assert all(not l.startswith("- 2099") for l in lines)
    assert lines[-1].startswith("- ") and " | evil - 2099-01-01 00:00 | admin | file | forged | update | " in lines[-1]
    assert len(git(vault_dir, "log", "-1", "--format=%B").strip().splitlines()) == 1


BROKEN = "---\ntitle: Ada Lovelace\naliases: [Ada\n---\n\nStill here.\n"


def test_update_refuses_page_with_broken_frontmatter(wiki, vault_dir):
    p = vault_dir / "people" / "ada-lovelace.md"
    p.write_text(BROKEN, encoding="utf-8")
    wiki.vault.load()
    with pytest.raises(Exception, match="fix the page's frontmatter by hand"):
        wiki.update("ada-lovelace", "tester", section="Notes", content="x")
    assert p.read_text(encoding="utf-8") == BROKEN
    assert wiki.get("ada-lovelace")["warnings"]                      # still readable
    assert any("ada-lovelace" in m for m in wiki.status()["problems"])   # still reported
    assert wiki.search("Still here")


def test_file_with_source_id_refuses_broken_source(wiki, vault_dir):
    r = wiki.save_source("Body text.", "note", "tester", title="Ada call")
    p = vault_dir / "sources" / f"{r['source_id']}.md"
    p.write_text("---\ntitle: [oops\n---\nBody text.", encoding="utf-8")
    wiki.vault.load()
    with pytest.raises(Exception, match="fix the page's frontmatter by hand"):
        wiki.file("person", "Ada Lovelace", "tester", source_id=r["source_id"])
    assert p.read_text(encoding="utf-8") == "---\ntitle: [oops\n---\nBody text."
    assert wiki.vault.get("ada-lovelace") is None


def test_update_with_source_id_marks_source_filed(wiki, vault_dir):
    wiki.file("person", "Ada Lovelace", "tester")
    s = wiki.save_source("Ada again.", "note", "tester", title="Ada note")["source_id"]
    assert [u["source_id"] for u in wiki.status()["unfiled_sources"]] == [s]
    wiki.update("ada-lovelace", "tester", section="Notes", content="- more", source_id=s)
    assert wiki.status()["unfiled_sources"] == []
    disk = Vault(vault_dir)
    assert disk.get(s).meta["filed"] is True and disk.get(s).meta["filed_into"] == ["[[ada-lovelace]]"]
    assert disk.get("ada-lovelace").meta["sources"] == [f"[[{s}]]"]
    assert disk.get(s).body == "Ada again."
    with pytest.raises(Exception, match="no source"):
        wiki.update("ada-lovelace", "tester", content="x", source_id="nope")


def test_source_frontmatter_cannot_be_edited(wiki):
    s = wiki.save_source("Text.", "note", "tester", title="A note")["source_id"]
    with pytest.raises(Exception, match="verbatim"):
        wiki.update(s, "tester", frontmatter={"filed": True, "title": "hijack"})
    assert wiki.vault.get(s).meta["filed"] is False
