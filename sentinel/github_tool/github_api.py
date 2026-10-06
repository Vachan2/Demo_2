"""
GitHub API backend for Sentinel remediation (real GitHub integration).

Uses PyGitHub to create a branch off main, push patched files, and open
a pull request.  Requires GITHUB_TOKEN and GITHUB_REPO env vars.
"""
from __future__ import annotations

import base64
import logging

from config import settings

log = logging.getLogger("sentinel.github_tool.github_api")

try:
    from github import Github, GithubException  # type: ignore
    _GITHUB_AVAILABLE = True
except ImportError:
    _GITHUB_AVAILABLE = False
    log.warning("pygithub not installed — github_api backend disabled")

_g: "Github | None" = None
_repo_obj = None


def _client():
    global _g, _repo_obj
    if not _GITHUB_AVAILABLE:
        raise RuntimeError("pygithub not installed")
    if _g is None:
        _g = Github(settings.github_token)
    if _repo_obj is None:
        _repo_obj = _g.get_repo(settings.github_repo)
    return _repo_obj


def create_branch(branch_name: str, base: str = "main") -> bool:
    try:
        repo = _client()
        base_sha = repo.get_branch(base).commit.sha
        repo.create_git_ref(f"refs/heads/{branch_name}", base_sha)
        log.info("GitHub branch created", extra={"branch": branch_name})
        return True
    except Exception as exc:
        # Branch may already exist — try to delete and recreate
        try:
            repo = _client()
            ref = repo.get_git_ref(f"heads/{branch_name}")
            ref.delete()
            base_sha = repo.get_branch(base).commit.sha
            repo.create_git_ref(f"refs/heads/{branch_name}", base_sha)
            return True
        except Exception as exc2:
            log.error("create_branch failed", extra={"error": str(exc2)})
            return False


def update_file(relative_path: str, content: str, branch_name: str) -> bool:
    """Create or update *relative_path* on *branch_name*."""
    try:
        repo = _client()
        encoded = base64.b64encode(content.encode()).decode()
        # Check if file already exists (needed for update vs create)
        try:
            existing = repo.get_contents(relative_path, ref=branch_name)
            repo.update_file(
                relative_path,
                f"fix: update {relative_path}",
                content,
                existing.sha,
                branch=branch_name,
            )
        except Exception:
            repo.create_file(
                relative_path,
                f"fix: add {relative_path}",
                content,
                branch=branch_name,
            )
        log.info("GitHub file updated", extra={"file": relative_path})
        return True
    except Exception as exc:
        log.error("update_file failed", extra={"error": str(exc)})
        return False


def commit(message: str) -> bool:
    # GitHub API commits are created per file_update — no separate commit step
    return True


def create_pull_request(
    branch_name: str,
    title: str,
    body: str,
    base: str = "main",
) -> str:
    try:
        repo = _client()
        pr = repo.create_pull(
            title=title,
            body=body,
            head=branch_name,
            base=base,
        )
        log.info(
            "GitHub PR created",
            extra={"pr_number": pr.number, "url": pr.html_url},
        )
        return pr.html_url
    except Exception as exc:
        log.error("create_pull_request failed", extra={"error": str(exc)})
        return f"https://github.com/{settings.github_repo}/pull/error"
