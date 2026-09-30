from wikicore.init_vault import init_vault
from wikicore.schema import TypeSchema, load_schemas


def test_init_vault_creates_defaults(tmp_path):
    root = init_vault(tmp_path / "v")
    for f in ("_instructions.md", "_tools.yaml", "README.md", "index.md", "log.md"):
        assert (root / f).is_file(), f
    assert (root / "people" / ".gitkeep").exists()
    assert (root / "sources" / ".gitkeep").exists()


def test_load_schemas_default_types(tmp_path):
    schemas, problems = load_schemas(init_vault(tmp_path / "v"))
    assert problems == []
    assert set(schemas) == {"person", "org", "meeting", "project", "concept", "decision", "paper", "learning", "source"}
    assert schemas["person"].folder == "people"
    assert "What I want from them" in schemas["person"].sections


def test_validate_and_skeleton():
    s = TypeSchema.from_yaml({"name": "meeting", "folder": "meetings", "description": "x",
                              "fields": {"date": "YYYY-MM-DD"}, "required": ["date"], "sections": ["Summary", "Follow-ups"]})
    assert s.validate({}) == ["missing field `date` (YYYY-MM-DD)"]
    assert s.validate({"date": "2026-01-01"}) == []
    assert s.skeleton() == "## Summary\n\n## Follow-ups\n"


def test_broken_schema_file_is_a_problem_not_a_crash(tmp_path):
    root = init_vault(tmp_path / "v")
    (root / "_schema" / "bad.yaml").write_text("name: [unclosed", encoding="utf-8")
    schemas, problems = load_schemas(root)
    assert "person" in schemas and len(problems) == 1 and problems[0].startswith("bad.yaml")


def test_to_yaml_roundtrip():
    s = TypeSchema.from_yaml({"name": "talk", "description": "A talk.", "sections": ["Gist"]})
    import yaml
    assert TypeSchema.from_yaml(yaml.safe_load(s.to_yaml())) == s
    assert s.folder == "talk"
