from wikicore.cli import check_vault, main
from wikicore.init_vault import init_vault


def test_init(tmp_path, capsys):
    assert main(["init", str(tmp_path / "v")]) == 0
    assert (tmp_path / "v" / "_schema" / "person.yaml").exists()


def test_init_keeps_customised_files(tmp_path):
    v = init_vault(tmp_path / "v")
    custom = {"_instructions.md": "mine\n", "_tools.yaml": "search: {}\n", "README.md": "my readme\n",
              "_schema/person.yaml": "name: person\nfolder: people\n"}
    for name, text in custom.items():
        (v / name).write_text(text, encoding="utf-8")
    (v / "_schema" / "concept.yaml").unlink()
    assert main(["init", str(v)]) == 0
    for name, text in custom.items():
        assert (v / name).read_text(encoding="utf-8") == text
    assert (v / "_schema" / "concept.yaml").exists()   # a missing default is restored


def test_check_reports_problems(vault_dir, write_page):
    write_page("people", "ada-lovelace", "title: Ada", "Knows [[charles-babbage]].")
    (vault_dir / "people" / "broken.md").write_text("---\ntitle: [x\n---\nbody", encoding="utf-8")
    write_page("meetings", "2026-01-01-ada", "title: Ada call")   # meeting without required date
    problems = check_vault(vault_dir)
    assert any("charles-babbage" in p for p in problems)
    assert any(p.startswith("broken: frontmatter unreadable") for p in problems)
    assert any("missing field `date`" in p for p in problems)


def test_check_clean_vault_exit_code(vault_dir, monkeypatch, capsys):
    assert check_vault(vault_dir) == []
    monkeypatch.setenv("WIKICORE_VAULT", str(vault_dir))
    assert main(["check"]) == 0
    assert capsys.readouterr().out.strip() == "ok"


def test_check_exit_code_on_problems_and_bad_schema(vault_dir, write_page, monkeypatch):
    (vault_dir / "_schema" / "bad.yaml").write_text("- not: [a mapping", encoding="utf-8")
    monkeypatch.setenv("WIKICORE_VAULT", str(vault_dir))
    assert any("bad.yaml" in p for p in check_vault(vault_dir))
    assert main(["check"]) == 1


def test_check_missing_vault_does_not_crash(tmp_path):
    assert isinstance(check_vault(tmp_path / "nope"), list)


def test_token_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WIKICORE_VAULT", str(tmp_path / "v"))
    monkeypatch.setenv("WIKICORE_STATE", str(tmp_path / "state"))
    assert main(["token", "add", "claude-code"]) == 0
    assert capsys.readouterr().out.strip().startswith("wk_")
    main(["token", "list"])
    assert "claude-code" in capsys.readouterr().out
    assert main(["token", "revoke", "claude-code"]) == 0
    assert main(["token", "revoke", "claude-code"]) == 1


def test_oauth_revoke_all_uses_provider(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WIKICORE_VAULT", str(tmp_path / "v"))
    monkeypatch.setenv("WIKICORE_STATE", str(tmp_path / "state"))
    calls = []
    from wikicore import oauth
    monkeypatch.setattr(oauth.OAuthProvider, "revoke_all", lambda self: calls.append(1) or 3)
    assert main(["oauth", "revoke-all"]) == 0
    assert calls == [1] and "3 tokens revoked" in capsys.readouterr().out
