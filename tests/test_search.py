import pytest

from wikicore.config import Config
from wikicore.search import mentions, search
from wikicore.vault import Vault
from wikicore.wiki import Wiki


@pytest.fixture
def seeded(vault_dir, write_page):
    write_page("people", "ada-lovelace", "title: Ada Lovelace\naliases: [Ada]\nprojects: ['[[analytical-engine]]']",
               "## Who\n\nWrote the first program for the engine.")
    write_page("people", "charles-babbage", "title: Charles Babbage", "## Who\n\nDesigned the difference engine. Knows [[ada-lovelace]].")
    write_page("projects", "analytical-engine", "title: Analytical Engine\naliases: [AE]", "## Status\n\nBlueprints done.\n\n## Open\n\n- funding")
    write_page("concepts", "program", "title: Program", "A sequence of operations for the engine.")
    return vault_dir


@pytest.fixture
def wiki(seeded, tmp_path):
    return Wiki(Config(vault=seeded, state_dir=tmp_path / "state", git_push=False, dev_client="tester"))


def test_title_beats_body(seeded):
    hits = search(Vault(seeded), "ada")
    assert hits[0]["slug"] == "ada-lovelace"
    assert hits[0]["snippet"]


def test_all_terms_ranked_first_and_type_filter(seeded):
    v = Vault(seeded)
    assert search(v, "engine program")[0]["slug"] in {"program", "ada-lovelace"}
    assert {h["type"] for h in search(v, "engine", type="person")} == {"person"}


def test_project_filter(seeded):
    got = {h["slug"] for h in search(Vault(seeded), "engine", project="analytical-engine")}
    assert got == {"analytical-engine", "ada-lovelace"}


def test_prefix_matches_plurals(seeded):
    assert search(Vault(seeded), "blueprint")[0]["slug"] == "analytical-engine"


def test_mentions_finds_names_and_aliases(seeded):
    got = {m["slug"] for m in mentions(Vault(seeded), "Call with Ada about the AE budget and Charles Babbage.")}
    assert got == {"ada-lovelace", "analytical-engine", "charles-babbage"}


def test_wiki_read_ops(wiki):
    assert wiki.search("zzzz")["hint"]
    g = wiki.get("ada-lovelace")
    assert g["found"] and g["backlinks"] == ["charles-babbage"]
    assert wiki.get("ada-lovelac")["did_you_mean"][0] == "ada-lovelace"
    rel = wiki.related("ada-lovelace")
    assert {n["slug"] for n in rel["links_to"]} == {"analytical-engine"}
    assert {n["slug"] for n in rel["linked_from"]} == {"charles-babbage"}
    assert [n["slug"] for n in wiki.list_notes("person", {"projects": "[[analytical-engine]]"})["notes"]] == ["ada-lovelace"]
    assert "person" in wiki.schema()["types"]
    assert wiki.status()["pages"]["person"] == 2


def test_recent_parses_log(wiki):
    (wiki.root / "log.md").write_text("# Log\n\n- 2026-01-01 10:00 | tester | file | ada-lovelace\n- 2026-01-02 11:00 | tester | update | program §Definition\n", encoding="utf-8")
    r = wiki.recent(5)["changes"]
    assert r[0] == {"when": "2026-01-02 11:00", "client": "tester", "action": "update", "summary": "program §Definition"}
    assert len(r) == 2


def test_non_latin_and_accents(vault_dir, write_page):
    write_page("people", "li-lei", "title: 李雷", "## Who\n\nA founder.")
    write_page("people", "zoe-b", "title: Zoë B\naliases: [Zoe Bee]", "Text.")
    write_page("people", "plain", "title: Plain", "Met zoë once.")
    v = Vault(vault_dir)
    assert search(v, "李雷")[0]["slug"] == "li-lei"
    assert [m["slug"] for m in mentions(v, "met 李雷 today")] == ["li-lei"]
    assert search(v, "zoe")[0]["slug"] == "zoe-b"
    assert search(v, "zoë")[0]["slug"] == "zoe-b"
    assert "zoe-b" in {m["slug"] for m in mentions(v, "lunch with Zoë B")}
