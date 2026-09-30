import pytest

from wikicore.vault import (Note, Vault, VaultError, check_slug, edit_section, get_section, links_in,
                            merge_frontmatter, render_index, slugify)


def test_slugify():
    assert slugify("Ada Lovelace") == "ada-lovelace"
    assert slugify("Çédric & Co.") == "cedric-co"
    assert slugify("   ") == "untitled"
    assert len(slugify("x" * 300)) == 80


def test_check_slug_rejects_traversal():
    for bad in ("../../etc/passwd", "Ada Lovelace", "", "-x", "a/b"):
        with pytest.raises(VaultError):
            check_slug(bad)
    assert check_slug("ada-lovelace") == "ada-lovelace"


def test_links_in_nested():
    assert links_in({"org": "[[Analytical Engine]]", "with": ["[[ada-lovelace|Ada]]", 3]}) == ["analytical-engine", "ada-lovelace"]


def test_load_parse_and_backlinks(vault_dir, write_page):
    write_page("people", "ada-lovelace", "title: Ada Lovelace\naliases: [Ada, Countess of Lovelace]\norg: '[[analytical-engine]]'",
               "## Who\n\nMathematician.\n")
    write_page("orgs", "analytical-engine", "title: Analytical Engine")
    v = Vault(vault_dir)
    ada = v.get("ada-lovelace")
    assert ada.type == "person" and ada.title == "Ada Lovelace"
    assert ada.summary() == "Mathematician."
    assert v.backlinks["analytical-engine"] == {"ada-lovelace"}
    assert v.find_by_name("countess of lovelace").slug == "ada-lovelace"
    assert v.find_by_name("Ada", type="org") is None
    assert "ada-lovelace" in v.suggest("ada lovelac")


def test_malformed_frontmatter_loads_with_warning(vault_dir):
    (vault_dir / "people" / "broken.md").write_text("---\ntitle: [unclosed\n---\n\nStill here.\n", encoding="utf-8")
    v = Vault(vault_dir)
    n = v.get("broken")
    assert n is not None and "Still here." in n.body
    assert n.warnings and n.warnings[0].startswith("frontmatter unreadable")


def test_write_roundtrip_and_dates_are_strings(vault_dir):
    v = Vault(vault_dir)
    p = v.write(Note("ada-lovelace", "person", {"title": "Ada Lovelace", "created": "2026-01-01"}, "## Who\n\nHi"))
    assert p == vault_dir / "people" / "ada-lovelace.md"
    v2 = Vault(vault_dir)
    assert v2.get("ada-lovelace").meta["created"] == "2026-01-01"
    assert v2.get("ada-lovelace").body.strip() == "## Who\n\nHi"


def test_write_unknown_type_raises(vault_dir):
    with pytest.raises(VaultError, match="unknown type"):
        Vault(vault_dir).write(Note("x", "spaceship", {}, ""))


def test_edit_section_append_replace_missing():
    body = "## Who\n\nA.\n\n## Open threads\n\n- one\n\n## Timeline\n"
    b = edit_section(body, "open threads", "- two", "append")
    assert get_section(b, "Open threads") == "- one\n- two"
    b = edit_section(b, "Open threads", "- only", "replace")
    assert get_section(b, "Open threads") == "- only"
    assert get_section(b, "Who") == "A."
    b = edit_section(b, "Follow-ups", "- call", "append")
    assert b.rstrip().endswith("## Follow-ups\n\n- call")
    assert get_section(b, "Nope") is None


def test_merge_frontmatter():
    meta = {"title": "Ada", "aliases": ["A"], "role": "x", "type": "person", "added_by": "me"}
    out = merge_frontmatter(meta, {"aliases": ["A", "B"], "role": None, "type": "org", "added_by": "spoof", "org": "[[e]]"})
    assert out == {"title": "Ada", "aliases": ["A", "B"], "type": "person", "added_by": "me", "org": "[[e]]"}


def test_render_index_groups_by_type(vault_dir, write_page):
    write_page("people", "ada-lovelace", "title: Ada Lovelace", "Mathematician.")
    idx = render_index(Vault(vault_dir))
    assert "## person" in idx and "- [[ada-lovelace]] Ada Lovelace: Mathematician." in idx


def test_dangling_links(vault_dir, write_page):
    write_page("people", "ada-lovelace", "title: Ada", "Knows [[charles-babbage]].")
    assert Vault(vault_dir).dangling() == [("ada-lovelace", "charles-babbage")]


def test_check_slug_rejects_trailing_newline():
    with pytest.raises(VaultError):
        check_slug("ada\n")


def test_norm_name_keeps_non_latin_names_distinct():
    from wikicore.vault import norm_name
    assert norm_name("李雷") != norm_name("王芳")
    assert norm_name("李雷") != norm_name("untitled")
    assert norm_name("Ada Lovelace") == "ada lovelace"


def test_section_ends_at_h1():
    b = edit_section("## A\n\nx\n\n# Top\n\nt\n", "A", "y", "append")
    assert get_section(b, "A") == "x\ny"
    assert get_section(b, "Top") == "t"


def test_section_ignores_fenced_headings():
    body = "## A\n\n```\n## x\n```\n\nmore\n\n## B\n\nb\n"
    assert get_section(body, "A") == "```\n## x\n```\n\nmore"
    assert get_section(body, "x") is None


def test_subsection_stays_inside_parent():
    body = "## Parent\n\np\n\n### Child\n\nc\n\n## Next\n\nn\n"
    assert get_section(body, "Parent") == "p\n\n### Child\n\nc"
    assert get_section(body, "Child") == "c"
