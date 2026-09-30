"""Git plumbing for the vault: pull before writing, commit as the client, push best-effort.

No call here raises: a timeout, a missing git binary or a failing command comes back as an error text, so a
write is never lost to a broken remote.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class Git:
    def __init__(self, root: Path, push: bool = True, timeout: int = 60):
        self.root = Path(root)
        self.push_enabled = push
        self.timeout = timeout
        self.conflict = False        # the last pull hit a rebase conflict: vault and remote have diverged
        self.commit_error: str | None = None

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "GCM_INTERACTIVE": "never"}
        try:
            return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=self.timeout, env=env)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 124, "", f"git {args[0]} timed out after {self.timeout}s")
        except OSError as e:
            return subprocess.CompletedProcess(args, 127, "", f"git {args[0]} could not run: {e}")

    def is_repo(self) -> bool:
        return (self.root / ".git").exists()

    def has_remote(self) -> bool:
        return self.is_repo() and bool(self._run("remote").stdout.strip())

    def head(self) -> str:
        return self._run("rev-parse", "HEAD").stdout.strip() if self.is_repo() else ""

    def branch(self) -> str:
        return self._run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def has_upstream(self) -> bool:
        return self._run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").returncode == 0

    def _rebase_in_progress(self) -> bool:
        return any((self.root / ".git" / d).exists() for d in ("rebase-merge", "rebase-apply"))

    def pull(self) -> str | None:
        self.conflict = False
        if not self.has_remote():
            return None
        if self.has_upstream():
            r = self._run("pull", "-q", "--rebase", "--autostash")
        else:
            branch = self.branch()
            probe = self._run("ls-remote", "--exit-code", "--heads", "origin", branch)
            if probe.returncode == 2:
                return None          # the remote does not have this branch yet: nothing to pull
            if probe.returncode:
                return (probe.stderr or probe.stdout).strip() or "pull failed"
            r = self._run("pull", "-q", "--rebase", "--autostash", "origin", branch)
        if r.returncode:
            self.conflict = self._rebase_in_progress()
            if self.conflict:
                self._run("rebase", "--abort")
            return (r.stderr or r.stdout).strip() or "pull failed"
        return None

    def commit(self, paths: list[str], message: str, author: str) -> bool:
        self.commit_error = None
        if not self.is_repo():
            return False
        self._run("add", "--", *paths)
        if self._run("diff", "--cached", "--quiet").returncode == 0:
            return False
        r = self._run("-c", f"user.name={author}", "-c", f"user.email={author}@wikicore.invalid", "commit", "-q", "-m", message)
        if r.returncode:
            self.commit_error = (r.stderr or r.stdout).strip() or "commit failed"
        return r.returncode == 0

    def push(self) -> str | None:
        if not self.push_enabled or not self.has_remote():
            return None
        r = self._run("push", "-q") if self.has_upstream() else self._run("push", "-q", "-u", "origin", "HEAD")
        return (r.stderr or r.stdout).strip() or "push failed" if r.returncode else None
