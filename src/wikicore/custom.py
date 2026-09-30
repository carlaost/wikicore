"""Vault-driven customisation: server name and instructions (_instructions.md), tool descriptions (_tools.yaml)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter
import yaml

log = logging.getLogger(__name__)


@dataclass
class Custom:
    name: str = "wikicore"
    instructions: str = ""
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_custom(root: Path) -> Custom:
    c = Custom()
    p = Path(root) / "_instructions.md"
    if p.exists():
        try:
            post = frontmatter.loads(p.read_text(encoding="utf-8"))
            c.name = str(post.metadata.get("name") or c.name)
            c.instructions = post.content.strip()
        except Exception:  # noqa: BLE001
            log.warning("_instructions.md frontmatter unreadable; using its raw text")
            c.instructions = p.read_text(encoding="utf-8").strip()
    t = Path(root) / "_tools.yaml"
    if t.exists():
        try:
            raw = yaml.safe_load(t.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            log.warning("_tools.yaml unreadable; using default tool descriptions")
            raw = {}
        if not isinstance(raw, dict):
            log.warning("_tools.yaml must be a mapping of tool name to settings; ignoring it")
            raw = {}
        for name, cfg in raw.items():
            if not isinstance(cfg, dict):
                log.warning("_tools.yaml: entry `%s` must be a mapping; ignoring it", name)
                continue
            if not isinstance(cfg.get("parameters") or {}, dict):
                log.warning("_tools.yaml: `%s.parameters` must be a mapping; ignoring it", name)
                cfg = {k: v for k, v in cfg.items() if k != "parameters"}
            c.tools[str(name)] = cfg
    return c


def tool_description(name: str, default: str, custom: Custom) -> str | None:
    cfg = custom.tools.get(name) or {}
    if cfg.get("disabled"):
        return None
    d = str(cfg.get("description") or default)
    if cfg.get("description_append"):
        d = f"{d} {cfg['description_append']}"
    return d


def param_overrides(name: str, custom: Custom) -> dict[str, str]:
    return {str(k): str(v) for k, v in ((custom.tools.get(name) or {}).get("parameters") or {}).items()}
