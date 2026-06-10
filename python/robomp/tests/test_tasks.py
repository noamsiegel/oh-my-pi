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


HEAD_SHA = "a" * 40
OLD_HEAD_SHA = "b" * 40


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
    def __init__(self, ci: PullRequestCiStatusInfo, reviews: list[PullRequestReviewInfo] | None = None) -> None:
        self.ci = ci
        self.reviews = reviews or []
        self.list_pr_reviews_called = False
        self.list_pr_files_called = False
        self.posted_comments: list[str] = []
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
            head_sha=HEAD_SHA,
            author="alice",
            author_type="User",
            head_repo=repo,
            title="PR",
            body="Body",
        )

    async def get_commit_ci_status(self, repo: str, head_sha: str) -> PullRequestCiStatusInfo:
        assert repo == "octo/widget"
        assert head_sha == HEAD_SHA
        return self.ci

    async def list_pr_reviews(self, repo: str, pr_number: int) -> list[PullRequestReviewInfo]:
        self.list_pr_reviews_called = True
        return self.reviews

    async def list_pr_files(self, repo: str, pr_number: int) -> list[PullRequestFileInfo]:
        self.list_pr_files_called = True
        return [PullRequestFileInfo(path="src/app.py", status="modified", additions=1, deletions=0)]

    async def post_comment(self, repo: str, number: int, body: str) -> CommentInfo:
        self.posted_comments.append(body)
        return CommentInfo(id=1, author="robomp-bot", body=body, created_at=_now_iso())


class _FakeDb:
    def __init__(self) -> None:
        self.issues: list[dict[str, object]] = []
        self.completed_review_queries: list[tuple[str, int, str]] = []
        self.successful_tool_call_queries: list[tuple[str, str]] = []

    def has_successful_tool_call(self, key: str, tool: str) -> bool:
        self.successful_tool_call_queries.append((key, tool))
        return True

    def has_completed_pr_review(self, repo: str, pr_number: int, head_sha: str) -> bool:
        self.completed_review_queries.append((repo, pr_number, head_sha))
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
        self.issues.append({"key": key, "repo": repo, "number": number, "state": state, **kwargs})


class _FakeSandbox:
    natives_cache = None

    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def ensure_workspace(self, **kwargs: object) -> SimpleNamespace:
        self.kwargs = kwargs
        return SimpleNamespace(branch="review", session_dir=Path("/tmp/session"), review_head_sha=kwargs.get("pr_head_sha"))


class _FakeGitTransport:
    pass


def _ci(state: str, *, total: int = 1, pending: int = 0, failed: int = 0) -> PullRequestCiStatusInfo:
    return PullRequestCiStatusInfo(
        head_sha=HEAD_SHA,
        state=state,  # type: ignore[arg-type]
        total_count=total,
        pending_count=pending,
        failed_count=failed,
    )

def _bot_review(state: str, commit_id: str) -> PullRequestReviewInfo:
    return PullRequestReviewInfo(
        id=100,
        author="robomp-bot",
        body="review body",
        state=state,
        submitted_at=_now_iso(),
        commit_id=commit_id,
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
async def test_review_pr_passes_head_sha_to_workspace_and_records_issue(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("passed", total=1))
    db = _FakeDb()
    sandbox = _FakeSandbox()
    calls: list[str] = []

    async def _run_task(**kwargs: object) -> None:
        calls.append(str(kwargs["task_kind"]))

    monkeypatch.setattr(tasks, "run_task", _run_task)

    outcome = await tasks.review_pr(
        settings=settings,
        db=db,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        sandbox=sandbox,  # type: ignore[arg-type]
        git_transport=_FakeGitTransport(),  # type: ignore[arg-type]
        payload=_payload(),
        delivery_id="delivery",
        received_at=_now_iso(),
    )

    assert outcome is None
    assert calls == ["review_pr"]
    assert sandbox.kwargs["pr_head_sha"] == HEAD_SHA
    assert any(row.get("review_head_sha") == HEAD_SHA and row.get("session_dir") for row in db.issues)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["APPROVED", "COMMENTED", "CHANGES_REQUESTED"])
async def test_review_pr_does_not_skip_new_head_for_old_completed_review(
    settings, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("passed", total=1), reviews=[_bot_review(state, OLD_HEAD_SHA)])
    db = _FakeDb()
    sandbox = _FakeSandbox()
    calls: list[str] = []

    async def _run_task(**kwargs: object) -> None:
        calls.append(str(kwargs["task_kind"]))

    monkeypatch.setattr(tasks, "run_task", _run_task)

    outcome = await tasks.review_pr(
        settings=settings,
        db=db,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        sandbox=sandbox,  # type: ignore[arg-type]
        git_transport=_FakeGitTransport(),  # type: ignore[arg-type]
        payload=_payload(),
        delivery_id="delivery",
        received_at=_now_iso(),
    )

    assert outcome is None
    assert calls == ["review_pr"]
    assert sandbox.kwargs["pr_head_sha"] == HEAD_SHA
    assert db.successful_tool_call_queries == []


@pytest.mark.asyncio
async def test_review_pr_sets_verify_fixes_focus_for_old_changes_requested(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("passed", total=1), reviews=[_bot_review("CHANGES_REQUESTED", OLD_HEAD_SHA)])
    captured = {}

    async def _run_task(**kwargs: object) -> None:
        captured["inputs"] = kwargs["inputs"]

    monkeypatch.setattr(tasks, "run_task", _run_task)

    outcome = await tasks.review_pr(
        settings=settings,
        db=_FakeDb(),  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        sandbox=_FakeSandbox(),  # type: ignore[arg-type]
        git_transport=_FakeGitTransport(),  # type: ignore[arg-type]
        payload=_payload(),
        delivery_id="delivery",
        received_at=_now_iso(),
    )

    assert outcome is None
    focus = captured["inputs"].pr_review_focus
    assert focus.mode == "verify-fixes"
    assert focus.prior_review_commit_id == OLD_HEAD_SHA
    assert focus.prior_review_id == 100
    assert focus.prior_review_submitted_at
    assert "verifying fixes" in github.posted_comments[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["APPROVED", "COMMENTED"])
async def test_review_pr_keeps_fresh_focus_for_old_commented_or_approved(
    settings, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("passed", total=1), reviews=[_bot_review(state, OLD_HEAD_SHA)])
    captured = {}

    async def _run_task(**kwargs: object) -> None:
        captured["inputs"] = kwargs["inputs"]

    monkeypatch.setattr(tasks, "run_task", _run_task)

    outcome = await tasks.review_pr(
        settings=settings,
        db=_FakeDb(),  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        sandbox=_FakeSandbox(),  # type: ignore[arg-type]
        git_transport=_FakeGitTransport(),  # type: ignore[arg-type]
        payload=_payload(),
        delivery_id="delivery",
        received_at=_now_iso(),
    )

    assert outcome is None
    assert captured["inputs"].pr_review_focus.mode == "fresh"


@pytest.mark.asyncio
async def test_review_pr_verify_fixes_focus_handles_missing_commit_id(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("passed", total=1), reviews=[_bot_review("CHANGES_REQUESTED", "")])
    captured = {}

    async def _run_task(**kwargs: object) -> None:
        captured["inputs"] = kwargs["inputs"]

    monkeypatch.setattr(tasks, "run_task", _run_task)

    outcome = await tasks.review_pr(
        settings=settings,
        db=_FakeDb(),  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        sandbox=_FakeSandbox(),  # type: ignore[arg-type]
        git_transport=_FakeGitTransport(),  # type: ignore[arg-type]
        payload=_payload(),
        delivery_id="delivery",
        received_at=_now_iso(),
    )

    assert outcome is None
    focus = captured["inputs"].pr_review_focus
    assert focus.mode == "verify-fixes"
    assert focus.prior_review_commit_id == ""


@pytest.mark.asyncio
async def test_review_pr_skips_same_head_changes_requested(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("passed", total=1), reviews=[_bot_review("CHANGES_REQUESTED", HEAD_SHA)])
    calls: list[str] = []

    async def _run_task(**kwargs: object) -> None:
        calls.append(str(kwargs["task_kind"]))

    monkeypatch.setattr(tasks, "run_task", _run_task)

    outcome = await _call_review(settings, github, received_at=_now_iso())

    assert outcome is not None
    assert outcome.state == "skipped"
    assert calls == []

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
