"""Note types, read from the vault's _schema/*.yaml. Adding a type is adding a file."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

SCHEMA_DIR = "_schema"


@dataclass
class TypeSchema:
    name: str
    folder: str
    description: str
    fields: dict[str, str] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, data: dict) -> TypeSchema:
        return cls(
            name=str(data["name"]),
            folder=str(data.get("folder") or data["name"]),
            description=str(data.get("description") or "").strip(),
            fields={str(k): str(v) for k, v in (data.get("fields") or {}).items()},
            required=[str(x) for x in data.get("required") or []],
            sections=[str(x) for x in data.get("sections") or []],
        )

    def to_yaml(self) -> str:
        return yaml.safe_dump({"name": self.name, "folder": self.folder, "description": self.description,
                               "fields": self.fields, "required": self.required, "sections": self.sections},
                              sort_keys=False, allow_unicode=True)

    def validate(self, meta: dict) -> list[str]:
        return [f"missing field `{f}` ({self.fields.get(f, '')})" for f in self.required if meta.get(f) in (None, "", [])]

    def skeleton(self) -> str:
        return "\n".join(f"## {s}\n" for s in self.sections)


def load_schemas(root: Path) -> tuple[dict[str, TypeSchema], list[str]]:
    schemas: dict[str, TypeSchema] = {}
    problems: list[str] = []
    for p in sorted((Path(root) / SCHEMA_DIR).glob("*.yaml")):
        try:
            s = TypeSchema.from_yaml(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
        except (yaml.YAMLError, KeyError, TypeError, AttributeError) as e:
            problems.append(f"{p.name}: {e}")
            continue
        schemas[s.name] = s
    return schemas, problems
