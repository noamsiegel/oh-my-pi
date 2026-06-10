"""Shared test fakes for robomp queue/server tests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from robomp.config import Settings
from robomp.db import Database, EventRow
from robomp.queue import WorkerPool
from robomp.slot_pool import SlotPool


class _Comment:
    id = 101


class RecordingGitHub:
    """GitHub fake that records issue/PR comments posted by code under test."""

    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []

    async def post_comment(self, repo: str, number: int, body: str) -> _Comment:
        self.comments.append((repo, number, body))
        return _Comment()

    async def get_repo(self, repo_full: str):
        from robomp.github_types import RepoInfo

        return RepoInfo(repo_full, "main", f"https://github.com/{repo_full}.git", False)

    async def get_issue(self, repo_full: str, number: int):
        from robomp.github_types import IssueInfo

        return IssueInfo(repo_full, number, "Test issue", "body", "open", "alice", ("robo-review",), True)

    async def get_pull_request(self, repo_full: str, number: int):
        from robomp.github_types import PullRequestInfo

        return PullRequestInfo(repo_full, number, f"https://github.com/{repo_full}/pull/{number}", "alice/fix", "main", "open", "alice", repo_full)


@dataclass(slots=True, frozen=True)
class RecordingWorkspace:
    branch: str
    session_dir: Path
    context_dir: Path
    repo_dir: Path
    review_head_sha: str | None = None


class RecordingSandbox:
    """SandboxManager fake: records workspace lifecycle calls."""

    natives_cache = None

    def __init__(self, tmp_root: Path | None = None) -> None:
        self.tmp_root = tmp_root or Path("/tmp/robomp-test-workspaces")
        self.ensure_calls: list[dict[str, Any]] = []
        self.remove_calls: list[tuple[str, int, str | None]] = []

    def ensure_workspace(
        self,
        *,
        repo: str,
        number: int,
        title: str,
        clone_url: str,
        default_branch: str,
        existing_branch: str | None = None,
        pr_head: int | None = None,
        pr_head_sha: str | None = None,
        pr_base_ref: str | None = None,
        pr_changed_paths: Iterable[str] | None = None,
        author_name: str = "",
        author_email: str = "",
        slot_uid: int | None = None,
    ) -> RecordingWorkspace:
        self.ensure_calls.append(
            {
                "repo": repo,
                "number": number,
                "title": title,
                "clone_url": clone_url,
                "default_branch": default_branch,
                "existing_branch": existing_branch,
                "pr_head": pr_head,
                "pr_head_sha": pr_head_sha,
                "pr_base_ref": pr_base_ref,
                "pr_changed_paths": tuple(pr_changed_paths or ()),
                "author_name": author_name,
                "author_email": author_email,
                "slot_uid": slot_uid,
            }
        )
        wid = f"{repo.replace('/', '__')}__{number}"
        if pr_head_sha is not None:
            wid = f"{wid}__{pr_head_sha}"
        return RecordingWorkspace(
            branch=existing_branch or (f"review/pr-{pr_head}" if pr_head is not None else f"farm/auto/{wid}"),
            session_dir=self.tmp_root / wid / "session",
            context_dir=self.tmp_root / wid / "context",
            repo_dir=self.tmp_root / wid / "repo",
            review_head_sha=pr_head_sha,
        )

    def remove_workspace(self, *, repo: str, number: int, head_sha: str | None = None) -> None:
        self.remove_calls.append((repo, number, head_sha))


class StubGitTransport:
    """Sentinel; tests using it do not push."""


def make_pool(
    settings: Settings,
    db: Database,
    *,
    github: object | None = None,
    sandbox: object | None = None,
    git_transport: object | None = None,
    slot_pool: SlotPool | None = None,
) -> WorkerPool:
    return WorkerPool(
        settings=settings,
        db=db,
        github=github if github is not None else RecordingGitHub(),  # type: ignore[arg-type]
        sandbox=sandbox if sandbox is not None else RecordingSandbox(),  # type: ignore[arg-type]
        git_transport=git_transport if git_transport is not None else StubGitTransport(),  # type: ignore[arg-type]
        slot_pool=slot_pool if slot_pool is not None else SlotPool(),
    )


def event_row_factory(
    delivery: str = "d1",
    *,
    event_type: str = "issues",
    repo: str = "octo/widget",
    issue_key: str = "octo/widget#1",
    payload: dict[str, Any] | None = None,
    received_at: str = "2026-01-01T00:00:00Z",
    state: str = "running",
    attempts: int = 1,
    last_error: str | None = None,
) -> EventRow:
    return EventRow(
        delivery_id=delivery,
        event_type=event_type,
        repo=repo,
        issue_key=issue_key,
        payload=payload or {"action": "opened"},
        received_at=received_at,
        state=state,
        attempts=attempts,
        last_error=last_error,
    )
