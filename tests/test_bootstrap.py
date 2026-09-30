import pytest
from mcp import Client

from test_server import data
from wikicore.bootstrap import guide_for
from wikicore.config import Config
from wikicore.server import build_server
from wikicore.wiki import Wiki


@pytest.fixture
def wiki(vault_dir, tmp_path):
    return Wiki(Config(vault=vault_dir, state_dir=tmp_path / "state", git_push=False, dev_client="claude-code"))


def test_guide_mapping():
    assert guide_for("Claude Code")[0] == "claude-code"
    assert guide_for("claude-code")[0] == "claude-code"
    assert guide_for("ChatGPT")[0] == "chatgpt"
    assert guide_for("claude")[0] == "claude-ai"
    assert guide_for("claude-ai")[0] == "claude-ai"
    assert guide_for("cursor")[0] == "other"
    assert "~/.claude/projects" in guide_for("claude-code")[1]
    assert "cannot search all past chats" in guide_for("chatgpt")[1]


def test_bootstrap_start_and_done(wiki, vault_dir):
    (vault_dir / "_instructions.md").write_text("---\nname: w\n---\nOnly engines.\n")
    r = wiki.bootstrap("claude-code")
    assert r["client"] == "claude-code" and r["rules"] == "Only engines." and r["previous_runs"] == []
    wiki.bootstrap("claude-code", done=True, summary="filed 12 pages from 9 memory files")
    assert (vault_dir / "_meta" / "bootstrap.yaml").exists()
    assert wiki.bootstrap("claude-code")["previous_runs"][0]["summary"] == "filed 12 pages from 9 memory files"
    assert "claude-code" in wiki.status()["bootstrap"]


def test_as_client_overrides(wiki):
    assert wiki.bootstrap("claude-code", as_client="chatgpt")["client"] == "chatgpt"


async def test_bootstrap_tool(wiki):
    async with Client(build_server(wiki)) as c:
        r = data(await c.call_tool("bootstrap", {}))
        assert r["client"] == "claude-code" and "steps" in r


def test_done_records_counts(wiki, vault_dir):
    wiki.bootstrap("claude-code", done=True, summary="ok", created=3, updated=4)
    wiki.bootstrap("claude-code", done=True, summary="again")
    runs = wiki.bootstrap("claude-code")["previous_runs"]
    assert (runs[0]["created"], runs[0]["updated"]) == (3, 4)
    assert "created" not in runs[1]


@pytest.mark.parametrize("content", ["a: [unclosed", "just: a mapping\n", "- notadict\n- client: x\n"])
def test_status_tolerates_bad_bootstrap_file(wiki, vault_dir, content):
    (vault_dir / "_meta").mkdir(exist_ok=True)
    (vault_dir / "_meta" / "bootstrap.yaml").write_text(content, encoding="utf-8")
    s = wiki.status()
    assert s["bootstrap"] == {}
    assert any(p.startswith("bootstrap:") for p in s["problems"])
    assert wiki.bootstrap("claude-code")["previous_runs"] == []


def test_done_refuses_to_overwrite_malformed_file(wiki, vault_dir):
    f = vault_dir / "_meta" / "bootstrap.yaml"
    f.parent.mkdir(exist_ok=True)
    f.write_text("a: [unclosed", encoding="utf-8")
    with pytest.raises(Exception, match="not valid YAML"):
        wiki.bootstrap("claude-code", done=True, summary="x")
    assert f.read_text(encoding="utf-8") == "a: [unclosed"
