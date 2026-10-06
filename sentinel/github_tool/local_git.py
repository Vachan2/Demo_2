"""
Local-git backend for Sentinel remediation (offline / demo mode).

Uses GitPython to create a branch, write patched files, commit, and return
a pseudo pull-request URL.  No network access required.
"""
from __future__ import annotations

import logging
from pathlib import Path

from config import settings

log = logging.getLogger("sentinel.github_tool.local_git")

try:
    import git as gitpython  # type: ignore
    _GIT_AVAILABLE = True
except ImportError:
    _GIT_AVAILABLE = False
    log.warning("gitpython not installed — local_git backend disabled")


def _repo() -> "gitpython.Repo":
    repo_path = Path(settings.local_repo_path).resolve()
    return gitpython.Repo(repo_path, search_parent_directories=True)


def create_branch(branch_name: str) -> bool:
    if not _GIT_AVAILABLE:
        return False
    try:
        repo = _repo()
        # If branch already exists, delete and recreate for clean demo runs
        if branch_name in [b.name for b in repo.branches]:
            repo.delete_head(branch_name, force=True)
        repo.create_head(branch_name)
        repo.heads[branch_name].checkout()
        log.info("branch created", extra={"branch": branch_name})
        return True
    except Exception as exc:
        log.error("create_branch failed", extra={"error": str(exc)})
        return False


def update_file(relative_path: str, content: str, branch_name: str) -> bool:
    """Write *content* to *relative_path* inside the repo working tree."""
    if not _GIT_AVAILABLE:
        return False
    try:
        repo = _repo()
        # Ensure we're on the right branch
        if repo.active_branch.name != branch_name:
            repo.heads[branch_name].checkout()
        fpath = Path(repo.working_dir) / relative_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")
        repo.index.add([str(fpath)])
        log.info("file staged", extra={"file": relative_path})
        return True
    except Exception as exc:
        log.error("update_file failed", extra={"error": str(exc)})
        return False


def commit(message: str) -> bool:
    if not _GIT_AVAILABLE:
        return False
    try:
        repo = _repo()
        if not repo.index.diff("HEAD"):
            log.info("nothing to commit")
            return True
        repo.index.commit(message)
        log.info("committed", extra={"message": message[:60]})
        return True
    except Exception as exc:
        log.error("commit failed", extra={"error": str(exc)})
        return False


def create_pull_request(
    branch_name: str,
    title: str,
    body: str,
    base: str = "main",
) -> str:
    """Return a pseudo local PR URL (no remote involved)."""
    if not _GIT_AVAILABLE:
        return f"local://pull/dry-run/{branch_name}"
    try:
        repo = _repo()
        # Count existing commits to generate a PR number
        pr_num = sum(1 for _ in repo.iter_commits()) % 1000 + 1
        url = f"local://pull/{pr_num}"
        log.info(
            "local PR created",
            extra={"url": url, "branch": branch_name, "title": title},
        )
        return url
    except Exception as exc:
        log.error("create_pull_request failed", extra={"error": str(exc)})
        return f"local://pull/error/{branch_name}"
