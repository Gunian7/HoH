from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Worktree:
    repo: Path
    path: Path
    branch: str


def create_worktree(repo: Path, path: Path, branch: str) -> Worktree:
    repo = Path(repo).resolve()
    path = Path(path).resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"not a git repository: {repo}")
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"worktree path is not empty: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(path), "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return Worktree(repo=repo, path=path, branch=branch)


def remove_worktree(worktree: Worktree) -> None:
    result = subprocess.run(
        ["git", "-C", str(worktree.repo), "worktree", "remove", "--force", str(worktree.path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
