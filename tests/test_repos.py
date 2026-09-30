import shutil
import subprocess

from wikicore.repos import Repos

import pytest
from mcp import Client

from conftest import git
from test_server import data
from wikicore.config import Config
from wikicore.server import build_server
from wikicore.wiki import Wiki


@pytest.fixture
def origin(tmp_path):
    bare = tmp_path / "engine.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    (work / "README.md").write_text("# Engine\n\nA calculating machine.\n")
    (work / "src").mkdir()
    (work / "src" / "mill.py").write_text("print('mill')\n")
    (work / "blob.bin").write_bytes(b"\0\1\2")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "first commit")
    git(work, "push", "-q", "origin", "HEAD")
    return bare, work


@pytest.fixture
def wiki(vault_dir, tmp_path, write_page):
    (vault_dir / "repos.yaml").write_text("repos: []\n")
    write_page("projects", "analytical-engine", "title: Analytical Engine", "## Status\n\nBlueprints done.\n\n## Open\n\n- funding")
    w = Wiki(Config(vault=vault_dir, state_dir=tmp_path / "state", git_push=False, dev_client="tester", repo_ttl=0))
    w.repos.allow_local = True   # the fixture origin is a filesystem path
    return w


def test_register_and_status(wiki, origin, vault_dir):
    bare, _ = origin
    r = wiki.register_repo(str(bare), "tester", project="analytical-engine")
    assert r["registered"] and r["name"] == "engine" and r["cloned"]
    assert "engine" in (vault_dir / "repos.yaml").read_text()
    s = wiki.repo_status("engine")
    assert s["commits"][0]["subject"] == "first commit"
    assert s["readme"].startswith("# Engine")
    assert s["project"]["status"] == "Blueprints done." and s["project"]["open"] == "- funding"
    with pytest.raises(Exception, match="already registered"):
        wiki.register_repo(str(bare), "tester")


def test_read_tree_log(wiki, origin):
    wiki.register_repo(str(origin[0]), "tester")
    assert wiki.repo_read("engine", "src/mill.py")["content"] == "print('mill')\n"
    assert {"name": "src", "kind": "dir"} in wiki.repo_tree("engine")["entries"]
    assert wiki.repo_log("engine", 5)["commits"][0]["subject"] == "first commit"
    with pytest.raises(Exception, match="binary"):
        wiki.repo_read("engine", "blob.bin")
    with pytest.raises(Exception, match="no file"):
        wiki.repo_read("engine", "nope.py")


def test_repo_read_rejects_escape(wiki, origin):
    wiki.register_repo(str(origin[0]), "tester")
    for bad in ("../../../etc/passwd", ".git/config", "/etc/passwd"):
        with pytest.raises(Exception, match="outside"):
            wiki.repo_read("engine", bad)


def test_new_commits_are_fetched(wiki, origin):
    bare, work = origin
    wiki.register_repo(str(bare), "tester")
    (work / "NEW.md").write_text("new")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "second commit")
    git(work, "push", "-q", "origin", "HEAD")
    assert wiki.repo_status("engine")["commits"][0]["subject"] == "second commit"


def test_stale_when_remote_gone(wiki, origin):
    bare, _ = origin
    wiki.register_repo(str(bare), "tester")
    shutil.rmtree(bare)
    s = wiki.repo_status("engine")
    assert s["commits"][0]["subject"] == "first commit"
    assert "fetch failed" in s["stale_warning"]


def test_unknown_repo(wiki):
    with pytest.raises(Exception, match="no repo `ghost`"):
        wiki.repo_status("ghost")


async def test_repo_tools_only_with_registry(wiki, vault_dir):
    async with Client(build_server(wiki)) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"repos", "register_repo", "repo_status", "repo_read", "repo_log", "repo_tree"} <= names
        assert data(await c.call_tool("repos"))["repos"] == []

def test_register_returns_name_without_rereading_yaml(wiki, origin):
    assert wiki.repos.register(str(origin[0]), name="Other Engine")[1] == "other-engine"


def test_fetch_timeout_serves_stale_clone(wiki, origin, monkeypatch):
    wiki.register_repo(str(origin[0]), "tester")
    real = subprocess.run

    def slow(argv, **kw):
        if "fetch" in argv:
            raise subprocess.TimeoutExpired(argv, 1)
        return real(argv, **kw)
    monkeypatch.setattr("wikicore.repos.subprocess.run", slow)
    s = wiki.repo_status("engine")
    assert s["commits"][0]["subject"] == "first commit"
    assert "timed out" in s["stale_warning"] and "fetched_at" in s


def test_clone_timeout_and_missing_git_do_not_raise(wiki, origin, monkeypatch):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    monkeypatch.setattr("wikicore.repos.subprocess.run", boom)
    r = wiki.register_repo(str(origin[0]), "tester")
    assert r["registered"] and not r["cloned"] and "timed out" in r["error"]
    monkeypatch.setattr("wikicore.repos.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git")))
    assert wiki.repos._git("status").returncode == 127


def test_git_never_prompts(tmp_path, monkeypatch):
    seen = {}

    def spy(argv, **kw):
        seen.update(kw["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr("wikicore.repos.subprocess.run", spy)
    Repos(tmp_path, tmp_path / "c")._git("status")
    assert seen["GIT_TERMINAL_PROMPT"] == "0"


def test_token_header_is_scoped_to_github(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("wikicore.repos.subprocess.run",
                        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    Repos(tmp_path, tmp_path / "c", token="sekret")._git("fetch")
    argv = calls[0]
    cfgs = [a for a in argv if a.startswith("http.")]
    assert len(cfgs) == 1 and cfgs[0].startswith("http.https://github.com/.extraHeader=")
    assert not any(a.startswith("http.extraHeader") for a in argv)
    calls.clear()
    Repos(tmp_path, tmp_path / "c")._git("fetch")
    assert not any("extraHeader" in a for a in calls[0])


@pytest.mark.parametrize("bad", ["--upload-pack=touch /tmp/x", "-oProxyCommand=x", "ext::sh -c id", "fd::3",
                                 "http://insecure/x", "file:///etc", "/srv/repo", "ftp://x/y", "https://x/a b"])
def test_register_rejects_dangerous_urls(tmp_path, bad):
    (tmp_path / "repos.yaml").write_text("repos: []\n")
    with pytest.raises(Exception, match="repo url|not a usable"):
        Repos(tmp_path, tmp_path / "c").register(bad)
    assert Repos(tmp_path, tmp_path / "c").load() == {}


def test_accepted_url_forms(tmp_path):
    (tmp_path / "repos.yaml").write_text("repos: []\n")
    r = Repos(tmp_path, tmp_path / "c")
    for u in ("https://github.com/ada/engine", "git@github.com:ada/looms.git", "ssh://git@host/ada/mill.git"):
        r.register(u)
    assert set(r.load()) == {"engine", "looms", "mill"}


def test_double_dash_guards_clone_and_ls_tree(wiki, origin, monkeypatch):
    wiki.register_repo(str(origin[0]), "tester")
    calls = []
    real = subprocess.run
    monkeypatch.setattr("wikicore.repos.subprocess.run", lambda argv, **kw: calls.append(argv) or real(argv, **kw))
    wiki.repo_tree("engine", "src")
    ls = next(a for a in calls if "ls-tree" in a)
    assert ls[-2:] == ["--", "src/"]
    shutil.rmtree(wiki.repos.clones / "engine")
    wiki.repos.fetched.clear()
    wiki.repo_status("engine")
    clone = next(a for a in calls if "clone" in a)
    assert clone[-3] == "--" and clone[-2] == str(origin[0])



def test_branch_injection_is_rejected(wiki, origin, vault_dir, tmp_path):
    bare, _ = origin
    wiki.register_repo(str(bare), "tester")
    pwned = tmp_path / "PWNED"
    (vault_dir / "repos.yaml").write_text(
        f"repos:\n- name: engine\n  url: {bare}\n  branch: '--upload-pack=touch {pwned};git-upload-pack'\n")
    wiki.repos.fetched.clear()
    with pytest.raises(Exception, match="no repo `engine`"):
        wiki.repo_status("engine")
    assert not pwned.exists()
    assert any("branch" in p for p in wiki.repos.problems)


@pytest.mark.parametrize("bad", ["-x", "a b", "a\\nb", "a..b", "x.lock", "a//b", "a/", "--upload-pack=x"])
def test_check_branch_rejects(bad):
    from wikicore.repos import check_branch
    with pytest.raises(Exception, match="branch"):
        check_branch(bad)


def test_fetch_argv_has_double_dash(wiki, origin, monkeypatch):
    wiki.register_repo(str(origin[0]), "tester")
    calls = []
    real = subprocess.run
    monkeypatch.setattr("wikicore.repos.subprocess.run", lambda argv, **kw: calls.append(argv) or real(argv, **kw))
    wiki.repo_status("engine")
    f = next(a for a in calls if "fetch" in a)
    assert f[f.index("--") + 1] == "origin"


def test_load_skips_bad_entries(tmp_path):
    (tmp_path / "repos.yaml").write_text(
        "repos:\n- name: good\n  url: https://github.com/ada/engine\n  branch: main\n"
        "- name: local\n  url: /srv/repo\n"
        "- name: fileurl\n  url: file:///etc\n"
        "- name: ../../x\n  url: https://github.com/ada/x\n"
        "- url: https://github.com/ada/noname\n"
        "- just a string\n")
    r = Repos(tmp_path, tmp_path / "c")
    assert list(r.load()) == ["good"]
    text = "\n".join(r.problems)
    assert "local" in text and "fileurl" in text and "../../x" in text and len(r.problems) == 5


@pytest.mark.parametrize("content", ["repos: [\n", "- a\n- b\n", "repos: 5\n", "repos:\n- url: x\n"])
def test_malformed_registry_never_breaks_status(wiki, vault_dir, content):
    (vault_dir / "repos.yaml").write_text(content)
    st = wiki.status()
    assert st["repos"] == [] and any(p.startswith("repos:") for p in st["problems"])
    assert wiki.repo_list()["repos"] == []
    with pytest.raises(Exception, match="no repo"):
        wiki.repo_status("engine")


def test_path_edge_cases(wiki, origin):
    wiki.register_repo(str(origin[0]), "tester")
    for bad in (".GIT/config", "src/../.Git/config"):
        with pytest.raises(Exception, match="outside"):
            wiki.repo_read("engine", bad)
    with pytest.raises(Exception, match="not valid"):
        wiki.repo_read("engine", "a.txt\0x")
    for bad in ("..", "../..", "../etc"):
        with pytest.raises(Exception, match="outside"):
            wiki.repo_tree("engine", bad)


def test_symlink_out_of_clone_rejected(wiki, origin):
    import os
    bare, work = origin
    os.symlink("/etc/hosts", work / "out")
    os.symlink("../../..", work / "updir")
    os.symlink("README.md", work / "README")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "links")
    git(work, "push", "-q", "origin", "HEAD")
    wiki.register_repo(str(bare), "tester")
    for bad in ("out", "updir/etc/hosts"):
        with pytest.raises(Exception, match="outside"):
            wiki.repo_read("engine", bad)
    assert wiki.repo_read("engine", "README")["content"].startswith("# Engine")


def test_read_is_bounded(wiki, origin):
    from wikicore.repos import MAX_BYTES
    bare, work = origin
    (work / "big.txt").write_text("x" * (MAX_BYTES + 500))
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "big")
    git(work, "push", "-q", "origin", "HEAD")
    wiki.register_repo(str(bare), "tester")
    r = wiki.repo_read("engine", "big.txt")
    assert r["truncated"] and len(r["content"]) == MAX_BYTES


def test_register_refuses_when_registry_has_unreadable_entries(wiki, origin, vault_dir):
    reg = vault_dir / "repos.yaml"
    reg.write_text("repos:\n- name: Notes Repo\n  url: https://github.com/example/notes\n", encoding="utf-8")
    before = reg.read_text(encoding="utf-8")
    with pytest.raises(Exception, match="fix repos.yaml first"):
        wiki.register_repo(str(origin[0]), "tester")
    assert reg.read_text(encoding="utf-8") == before
