import json

import pytest
from mcp import Client

from wikicore.config import Config
from wikicore.server import build_server
from wikicore.wiki import Wiki


def data(result):
    if result.structured_content:
        return result.structured_content.get("result", result.structured_content)
    return json.loads(result.content[0].text)


@pytest.fixture
def wiki(vault_dir, tmp_path):
    return Wiki(Config(vault=vault_dir, state_dir=tmp_path / "state", git_push=False, dev_client="tester"))


async def tools(server):
    async with Client(server) as c:
        return {t.name: t for t in (await c.list_tools()).tools}


async def test_core_tools_listed(wiki):
    names = set(await tools(build_server(wiki)))
    assert {"search", "get", "related", "list_notes", "recent", "schema", "status",
            "save_source", "file", "update", "add_type"} <= names
    assert not {"repos", "repo_status"} & names   # repo module off without repos.yaml


async def test_type_list_in_descriptions(wiki):
    t = await tools(build_server(wiki))
    assert "person (Someone the owner deals with)" in t["file"].description


async def test_vault_customisation(wiki, vault_dir):
    (vault_dir / "_instructions.md").write_text("---\nname: Engine wiki\n---\nOnly engines.\n")
    (vault_dir / "_tools.yaml").write_text(
        "search:\n  description_append: 'Use for anything about engines.'\n  parameters:\n    query: 'Engine words.'\n"
        "get:\n  description: 'Read an engine page.'\nadd_type:\n  disabled: true\n")
    server = build_server(wiki)
    assert server.name == "Engine wiki"
    assert server.instructions.strip() == "Only engines."
    t = await tools(server)
    assert "Use for anything about engines." in t["search"].description
    assert t["search"].input_schema["properties"]["query"]["description"] == "Engine words."
    assert t["get"].description == "Read an engine page."
    assert "add_type" not in t


async def test_add_type_refreshes_descriptions(wiki):
    server = build_server(wiki)
    async with Client(server) as c:
        await c.call_tool("add_type", {"name": "talk", "description": "A talk given or attended."})
        t = {x.name: x for x in (await c.list_tools()).tools}
        assert "talk (A talk given or attended)" in t["file"].description


async def test_paste_file_update_flow(wiki):
    async with Client(build_server(wiki)) as c:
        s = data(await c.call_tool("save_source", {"text": "Call with Ada. She wants the blueprints.", "kind": "granola", "title": "Ada call"}))
        assert s["saved"]
        f = data(await c.call_tool("file", {"type": "person", "title": "Ada Lovelace", "source_id": s["source_id"],
                                            "frontmatter": {"aliases": ["Ada"]}}))
        assert f["created"] and f["slug"] == "ada-lovelace"
        u = data(await c.call_tool("update", {"slug": "ada-lovelace", "section": "What they want from me", "content": "- the blueprints"}))
        assert u["updated"]
        g = data(await c.call_tool("get", {"slug": "ada-lovelace"}))
        assert "- the blueprints" in g["body"] and g["frontmatter"]["added_by"] == "tester"


async def test_errors_are_plain(wiki):
    async with Client(build_server(wiki)) as c:
        r = data(await c.call_tool("update", {"slug": "nobody", "content": "x"}))
        assert r == {"error": "no page `nobody`"}


async def test_update_and_schema_list_types(wiki):
    t = await tools(build_server(wiki))
    for name in ("update", "schema"):
        assert "person (Someone the owner deals with)" in t[name].description


async def test_unexpected_exception_becomes_plain_error(wiki, monkeypatch, caplog):
    def boom(slug):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(wiki, "get", boom)
    async with Client(build_server(wiki)) as c:
        r = data(await c.call_tool("get", {"slug": "x"}))
    assert r == {"error": "internal error: RuntimeError: kaboom"}
    assert "Traceback" in caplog.text


def test_reads_during_writes_are_consistent(wiki):
    import threading
    import time

    from wikicore.vault import Note
    import sys
    errors, stop = [], threading.Event()
    saved_interval = sys.getswitchinterval()

    def reader():
        while not stop.is_set():
            try:
                wiki.search("person")
                wiki.list_notes("person")
                wiki.status()
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                return
            time.sleep(0)   # yield the GIL so the writer is not starved

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for th in threads:
        th.start()
    try:
        sys.setswitchinterval(1e-5)
        for i in range(150):
            wiki.vault.write(Note(slug=f"person-{i}", type="person", meta={"type": "person", "title": f"Person {i}"}, body="x [[person-0]]"))
            if i % 30 == 0:
                wiki.vault.load()
    finally:
        stop.set()
        for th in threads:
            th.join()
        sys.setswitchinterval(saved_interval)
    assert errors == []


def test_filing_a_source_does_not_mutate_the_published_note(wiki):
    src = wiki.save_source("Call with Ada.", "note", "tester", "Ada call")["source_id"]
    before = wiki.vault.get(src)
    meta_before = dict(before.meta)
    wiki.file("person", "Ada Lovelace", "tester", source_id=src)
    assert before.meta == meta_before and before.meta["filed"] is False
    assert wiki.vault.get(src).meta["filed"] is True


@pytest.mark.parametrize("tools_yaml", [
    "- search\n- get\n",
    "search: hello\nget:\n  description: 'Read.'\n",
    "search:\n  parameters: [query]\n  description_append: 'Extra.'\n",
])
async def test_wrong_shaped_tools_yaml_is_skipped(wiki, vault_dir, tools_yaml):
    (vault_dir / "_tools.yaml").write_text(tools_yaml)
    server = build_server(wiki)
    t = await tools(server)
    assert "search" in t
    async with Client(server) as c:
        await c.call_tool("add_type", {"name": "talk", "description": "A talk."})


async def test_schema_change_hook_failure_does_not_fail_add_type(wiki):
    def bad():
        raise RuntimeError("hook")
    wiki.on_schema_change = bad
    async with Client(build_server(wiki)) as c:
        wiki.on_schema_change = bad
        r = data(await c.call_tool("add_type", {"name": "talk", "description": "A talk."}))
    assert r["added"]


def test_vault_write_leaves_its_argument_untouched(wiki):
    from wikicore.vault import Note
    note = Note("ada", "person", {"type": "person"}, "body")
    meta_before = dict(note.meta)
    wiki.vault.write(note)
    assert note.meta == meta_before and note.warnings == []
    assert wiki.vault.get("ada") is not note
