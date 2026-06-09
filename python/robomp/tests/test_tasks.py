from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from robomp import tasks
from robomp.github_types import (
    CommentInfo,
    IssueInfo,
    PullRequestCiStatusInfo,
    PullRequestFileInfo,
    PullRequestInfo,
    PullRequestReviewInfo,
    RepoInfo,
)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_comment_review_retryable_includes_uv_environment_failures() -> None:
    assert tasks._comment_review_retryable("Verification note: uv is not installed in this workspace.")
    assert tasks._comment_review_retryable("Verification note: `uv` is not installed in this workspace.")
    assert tasks._comment_review_retryable("error: command not found: uv")
    assert tasks._comment_review_retryable("local backend command could not run because uv is unavailable")
    assert tasks._comment_review_retryable("`manage.py`/`yarn` unavailable at expected paths")
    assert tasks._comment_review_retryable("error: command not found: yarn")
    assert not tasks._comment_review_retryable("review:clean — no blocking findings.")


def _old_iso(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _payload() -> dict[str, object]:
    return {"repository": {"full_name": "octo/widget"}, "pull_request": {"number": 9}}


class _FakeGitHub:
    def __init__(self, ci: PullRequestCiStatusInfo) -> None:
        self.ci = ci
        self.list_pr_reviews_called = False
        self.list_pr_files_called = False

    async def get_repo(self, repo: str) -> RepoInfo:
        return RepoInfo(full_name=repo, default_branch="main", clone_url="https://example/octo/widget.git", private=False)

    async def get_issue(self, repo: str, number: int) -> IssueInfo:
        return IssueInfo(
            repo=repo,
            number=number,
            title="PR",
            body="Body",
            state="open",
            author="alice",
            labels=("robo-review",),
            is_pull_request=True,
        )

    async def get_pull_request(self, repo: str, number: int) -> PullRequestInfo:
        return PullRequestInfo(
            repo=repo,
            number=number,
            html_url="https://example/pr/9",
            head_ref="feature",
            base_ref="main",
            state="open",
            draft=False,
            head_sha="abc123",
            author="alice",
            author_type="User",
            head_repo=repo,
            title="PR",
            body="Body",
        )

    async def get_commit_ci_status(self, repo: str, head_sha: str) -> PullRequestCiStatusInfo:
        assert repo == "octo/widget"
        assert head_sha == "abc123"
        return self.ci

    async def list_pr_reviews(self, repo: str, pr_number: int) -> list[PullRequestReviewInfo]:
        self.list_pr_reviews_called = True
        return []

    async def list_pr_files(self, repo: str, pr_number: int) -> list[PullRequestFileInfo]:
        self.list_pr_files_called = True
        return [PullRequestFileInfo(path="src/app.py", status="modified", additions=1, deletions=0)]

    async def post_comment(self, repo: str, number: int, body: str) -> CommentInfo:
        return CommentInfo(id=1, author="robomp-bot", body=body, created_at=_now_iso())


class _FakeDb:
    def __init__(self) -> None:
        self.issues: list[tuple[str, str]] = []

    def has_successful_tool_call(self, key: str, tool: str) -> bool:
        return False

    def reserve_side_effect(self, operation_key: str) -> bool:
        return True

    def side_effect_succeeded(self, operation_key: str) -> bool:
        return False

    def mark_side_effect_failed(self, operation_key: str, error: str) -> None:
        raise AssertionError(error)

    def mark_side_effect_succeeded(self, operation_key: str) -> None:
        pass

    def log_tool_call(self, **kwargs: object) -> None:
        pass

    def upsert_issue(self, *, key: str, repo: str, number: int, state: str, **kwargs: object) -> None:
        self.issues.append((key, state))


class _FakeSandbox:
    natives_cache = None

    def ensure_workspace(self, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(branch="review", session_dir=Path("/tmp/session"))


class _FakeGitTransport:
    pass


def _ci(state: str, *, total: int = 1, pending: int = 0, failed: int = 0) -> PullRequestCiStatusInfo:
    return PullRequestCiStatusInfo(
        head_sha="abc123",
        state=state,  # type: ignore[arg-type]
        total_count=total,
        pending_count=pending,
        failed_count=failed,
    )


async def _call_review(settings, github: _FakeGitHub, *, received_at: str | None) -> object:
    return await tasks.review_pr(
        settings=settings,
        db=_FakeDb(),  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        sandbox=_FakeSandbox(),  # type: ignore[arg-type]
        git_transport=_FakeGitTransport(),  # type: ignore[arg-type]
        payload=_payload(),
        delivery_id="delivery",
        received_at=received_at,
    )


@pytest.mark.asyncio
async def test_review_pr_defers_when_ci_pending_before_fetching_reviews_or_workspace(settings) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("pending", total=1, pending=1))

    outcome = await _call_review(settings, github, received_at=_now_iso())

    assert outcome is not None
    assert outcome.state == "queued"
    assert outcome.retry_limit is None
    assert "waiting for PR CI checks" in (outcome.reason or "")
    assert github.list_pr_reviews_called is False
    assert github.list_pr_files_called is False


@pytest.mark.asyncio
async def test_review_pr_skips_when_ci_failed(settings) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("failed", total=1, failed=1))

    outcome = await _call_review(settings, github, received_at=_now_iso())

    assert outcome is not None
    assert outcome.state == "skipped"
    assert (outcome.reason or "").startswith("skip: PR CI checks failed")
    assert github.list_pr_reviews_called is False


@pytest.mark.asyncio
async def test_review_pr_continues_when_ci_passed(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("passed", total=1))
    calls: list[str] = []

    async def _run_task(**kwargs: object) -> None:
        calls.append(str(kwargs["task_kind"]))

    monkeypatch.setattr(tasks, "run_task", _run_task)

    outcome = await _call_review(settings, github, received_at=_now_iso())

    assert outcome is None
    assert calls == ["review_pr"]
    assert github.list_pr_reviews_called is True
    assert github.list_pr_files_called is True


@pytest.mark.asyncio
async def test_review_pr_skips_when_ci_pending_timeout_elapsed(settings) -> None:
    settings.pr_review_ci_gate_enabled = True
    settings.pr_review_ci_gate_timeout_seconds = 60.0
    github = _FakeGitHub(_ci("pending", total=0, pending=0))

    outcome = await _call_review(settings, github, received_at=_old_iso(120))

    assert outcome is not None
    assert outcome.state == "skipped"
    assert (outcome.reason or "").startswith("skip: PR CI checks did not complete before timeout")
    assert github.list_pr_reviews_called is False
