"""Read-only access to code repos listed in the vault's repos.yaml. The server keeps shallow
clones and refetches when they are older than `ttl` seconds; a failed fetch serves the stale
clone with a warning instead of nothing."""

from __future__ import annotations

import base64
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from wikicore.vault import VaultError, check_slug, slugify

REGISTRY = "repos.yaml"
MAX_BYTES = 200_000
DEPTH = "200"
GITHUB = "https://github.com/"


def check_url(url: str, allow_local: bool = False) -> str:
    """A repo url must be https:// or ssh (git@host:path, ssh://). Anything git could read as an option or a
    transport helper (leading `-`, `::`, ext::) is refused."""
    if url.startswith("-") or "::" in url or any(c.isspace() for c in url):
        raise VaultError("that is not a usable repo url; give an https:// or git@ address")
    if not (url.startswith(("https://", "ssh://", "git@")) or (allow_local and Path(url).is_absolute())):
        raise VaultError("repo urls must start with https://, ssh:// or git@")
    return url


BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def check_branch(branch: str) -> str:
    """A branch is passed to git as an argument: no leading `-`, whitespace or odd characters, and a valid ref name."""
    if (not isinstance(branch, str) or not BRANCH_RE.fullmatch(branch) or ".." in branch or "//" in branch
            or "/." in branch or branch.endswith(("/", ".", ".lock"))):
        raise VaultError(f"`{branch}` is not a usable branch name")
    return branch


@dataclass
class Repo:
    name: str
    url: str
    project: str | None = None
    branch: str | None = None


class Repos:
    def __init__(self, vault_root: Path, clones_dir: Path, token: str | None = None, ttl: int = 300):
        self.vault_root = Path(vault_root)
        self.clones = Path(clones_dir)
        self.token = token
        self.ttl = ttl
        self.timeout = 120
        self.allow_local = False   # tests only: lets a filesystem path stand in for a remote
        self.fetched: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self.problems: list[str] = []   # registry entries or files that could not be used, from the last load()

    @property
    def registry(self) -> Path:
        return self.vault_root / REGISTRY

    @property
    def enabled(self) -> bool:
        return self.registry.exists()

    def load(self) -> dict[str, Repo]:
        """Valid registry entries. A broken file or entry is skipped and reported in `self.problems`, never raised."""
        problems: list[str] = []
        out: dict[str, Repo] = {}
        try:
            raw = yaml.safe_load(self.registry.read_text(encoding="utf-8")) if self.enabled else None
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
            problems.append(f"{REGISTRY} could not be read: {' '.join(str(e).split())[:200]}")
            raw = None
        entries = raw.get("repos") if isinstance(raw, dict) else None
        if raw is not None and not isinstance(raw, dict) or (isinstance(raw, dict) and entries is not None and not isinstance(entries, list)):
            problems.append(f"{REGISTRY} must be a mapping with a `repos:` list")
            entries = None
        for i, r in enumerate(entries or [], 1):
            try:
                if not isinstance(r, dict):
                    raise VaultError("not a mapping")
                name, url = r.get("name"), r.get("url")
                if not isinstance(name, str) or not isinstance(url, str):
                    raise VaultError("`name` and `url` are required text")
                check_slug(name)
                check_url(url, self.allow_local)
                branch = r.get("branch")
                project = r.get("project")
                repo = Repo(name, url, str(project) if project else None, check_branch(str(branch)) if branch else None)
                if name in out:
                    raise VaultError("duplicate name")
                out[name] = repo
            except VaultError as e:
                problems.append(f"{REGISTRY} entry {i} ({r.get('name') if isinstance(r, dict) else '?'}) skipped: {e}")
        self.problems = problems
        return out

    def _save(self, repos: dict[str, Repo]) -> None:
        body = {"repos": [{k: v for k, v in asdict(r).items() if v} for r in repos.values()]}
        self.registry.write_text(yaml.safe_dump(body, sort_keys=False, allow_unicode=True), encoding="utf-8")

    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        """Never raises: a timeout is rc 124, a missing git rc 127, so callers fall back to the stale clone."""
        extra: list[str] = []
        if self.token:
            basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            # scoped to github.com so a registered attacker url never receives the token
            extra = ["-c", f"http.{GITHUB}.extraHeader=Authorization: Basic {basic}"]
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "GCM_INTERACTIVE": "never"}
        try:
            return subprocess.run(["git", *extra, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=self.timeout, env=env)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 124, "", f"git {args[0]} timed out after {self.timeout}s")
        except OSError as e:
            return subprocess.CompletedProcess(args, 127, "", f"git {args[0]} could not run: {e}")

    def register(self, url: str, project: str | None = None, name: str | None = None) -> tuple[Path, str]:
        url = check_url(url.strip().rstrip("/"), self.allow_local)
        name = check_slug(slugify(name or url.rsplit("/", 1)[-1].removesuffix(".git")))
        repos = self.load()
        if self.problems:
            raise VaultError("repos.yaml has entries that could not be read (" + "; ".join(self.problems)
                             + "); fix repos.yaml first, then register again")
        if name in repos:
            raise VaultError(f"repo `{name}` is already registered")
        repos[name] = Repo(name=name, url=url, project=slugify(project) if project else None)
        self._save(repos)
        return self.registry, name

    def _repo(self, name: str) -> Repo:
        repos = self.load()
        if name not in repos:
            raise VaultError(f"no repo `{name}`; registered: {', '.join(repos) or 'none'}")
        return repos[name]

    def ensure(self, name: str) -> bool:
        repo = self._repo(name)
        d = self.clones / name
        cloned = (d / ".git").exists()
        if cloned and time.time() - self.fetched.get(name, 0) < self.ttl:
            return True
        self.clones.mkdir(parents=True, exist_ok=True)
        if not cloned:
            args = ["clone", "-q", "--depth", DEPTH] + (["--branch", repo.branch] if repo.branch else []) + ["--", repo.url, str(d)]
            r = self._git(*args)
        else:
            r = self._git("fetch", "-q", "--depth", DEPTH, "--", "origin", repo.branch or "HEAD", cwd=d)
            if r.returncode == 0:
                r = self._git("reset", "-q", "--hard", "FETCH_HEAD", cwd=d)
        if r.returncode:
            self.errors[name] = (r.stderr or r.stdout).strip()[-300:] or "git failed"
            return (d / ".git").exists()
        self.fetched[name] = time.time()
        self.errors.pop(name, None)
        return True

    def _dir(self, name: str) -> Path:
        if not self.ensure(name):
            raise VaultError(f"could not clone `{name}`: {self.errors.get(name, 'unknown error')}")
        return self.clones / name

    def _freshness(self, name: str) -> dict[str, Any]:
        at = self.fetched.get(name)
        out: dict[str, Any] = {"fetched_at": datetime.fromtimestamp(at).isoformat(timespec="minutes") if at else None}
        if name in self.errors:
            out["stale_warning"] = f"fetch failed ({self.errors[name]}); showing the last fetched state"
        return out

    def _commits(self, d: Path, n: int, path: str | None = None) -> list[dict[str, str]]:
        args = ["log", f"-n{n}", "--date=short", "--format=%h%x09%ad%x09%an%x09%s"] + (["--", path] if path else [])
        rows = [line.split("\t", 3) for line in self._git(*args, cwd=d).stdout.splitlines()]
        return [dict(zip(("sha", "date", "author", "subject"), r)) for r in rows if len(r) == 4]

    def list(self) -> list[dict[str, Any]]:
        return [{"name": r.name, "url": r.url, "project": r.project, **self._freshness(r.name)} for r in self.load().values()]

    def status(self, name: str, n: int = 10) -> dict[str, Any]:
        d = self._dir(name)
        readme = None
        for cand in ("README.md", "README", "readme.md"):
            try:
                f = self._safe(d, cand)
            except VaultError:
                continue
            if f.is_file():
                with f.open("rb") as fh:
                    readme = "\n".join(fh.read(MAX_BYTES + 1)[:MAX_BYTES].decode("utf-8", errors="replace").splitlines()[:60])
                break
        return {"name": name, "project": self._repo(name).project,
                "branch": self._git("rev-parse", "--abbrev-ref", "HEAD", cwd=d).stdout.strip(),
                "commits": self._commits(d, n),
                "readme": readme,
                **self._freshness(name)}

    def _safe(self, d: Path, path: str) -> Path:
        if "\0" in path:
            raise VaultError("that path is not valid")
        root = d.resolve()
        p = (root / path.lstrip()).resolve()
        if path.startswith("/") or not p.is_relative_to(root) or any(x.lower() == ".git" for x in p.relative_to(root).parts):
            raise VaultError(f"`{path}` is outside the repo")
        return p

    def read(self, name: str, path: str) -> dict[str, Any]:
        d = self._dir(name)
        p = self._safe(d, path)
        if not p.is_file():
            raise VaultError(f"no file `{path}` in `{name}`; use repo_tree to browse")
        with p.open("rb") as f:
            raw = f.read(MAX_BYTES + 1)
        if b"\0" in raw[:8000]:
            raise VaultError(f"`{path}` is a binary file")
        return {"name": name, "path": path, "content": raw[:MAX_BYTES].decode("utf-8", errors="replace"),
                "truncated": len(raw) > MAX_BYTES, **self._freshness(name)}

    def log(self, name: str, n: int = 20, path: str | None = None) -> dict[str, Any]:
        d = self._dir(name)
        if path:
            self._safe(d, path)
        return {"name": name, "commits": self._commits(d, n, path), **self._freshness(name)}

    def tree(self, name: str, path: str = "") -> dict[str, Any]:
        d = self._dir(name)
        prefix = ""
        if path.strip("/"):
            self._safe(d, path)
            prefix = path.strip("/") + "/"
        args = ["ls-tree", "HEAD"] + (["--", prefix] if prefix else [])
        out = self._git(*args, cwd=d).stdout.splitlines()
        entries = []
        for line in out:
            meta, fname = line.split("\t", 1)
            entries.append({"name": fname.removeprefix(prefix), "kind": "dir" if meta.split()[1] == "tree" else "file"})
        return {"name": name, "path": prefix, "entries": entries, **self._freshness(name)}
