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

from robomp.pr_review_tools import pr_review_comment_retryable
from robomp.db import PrReviewGateState, PrReviewGateTransition

HEAD_SHA = "a" * 40
OLD_HEAD_SHA = "b" * 40


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_comment_review_retryable_includes_uv_environment_failures() -> None:
    assert pr_review_comment_retryable("Verification note: uv is not installed in this workspace.")
    assert pr_review_comment_retryable("Verification note: `uv` is not installed in this workspace.")
    assert pr_review_comment_retryable("error: command not found: uv")
    assert pr_review_comment_retryable("local backend command could not run because uv is unavailable")
    assert pr_review_comment_retryable("`manage.py`/`yarn` unavailable at expected paths")
    assert pr_review_comment_retryable("error: command not found: yarn")
    assert not pr_review_comment_retryable("review:ready — no blocking findings.")


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
        self.submitted_reviews: list[dict[str, object]] = []
        self.updated_comments: list[tuple[str, int, str]] = []

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

    async def update_comment(self, repo: str, comment_id: int, body: str) -> CommentInfo:
        self.updated_comments.append((repo, comment_id, body))
        return CommentInfo(id=comment_id, author="robomp-bot", body=body, created_at=_now_iso())

    async def submit_pr_review(
        self,
        *,
        repo: str,
        pr_number: int,
        body: str,
        event: str,
        comments: list[object],
        commit_id: str | None = None,
    ) -> PullRequestReviewInfo:
        self.submitted_reviews.append(
            {"repo": repo, "pr_number": pr_number, "body": body, "event": event, "comments": comments, "commit_id": commit_id}
        )
        return PullRequestReviewInfo(
            id=900 + len(self.submitted_reviews),
            author="robomp-bot",
            body=body,
            state=event,
            submitted_at=_now_iso(),
            commit_id=commit_id or "",
        )


class _FakeDb:
    def __init__(self) -> None:
        self.issues: list[dict[str, object]] = []
        self.completed_review_queries: list[tuple[str, int, str]] = []
        self.successful_tool_call_queries: list[tuple[str, str]] = []
        self.gate: dict[str, dict[str, object]] = {}

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


    def list_pr_review_posted_findings(self, repo: str, pr_number: int) -> list[object]:
        return []

    def record_pr_review_completed_review(self, **kwargs: object) -> bool:
        return True

    def transition_pr_review_gate(
        self, *, issue_key, repo, pr_number, head_sha, gate_status, ci_signature=None,
    ) -> PrReviewGateTransition:
        prev = self.gate.get(issue_key) or {
            "status": "unknown", "episode": 0, "notified": 0, "sig": None, "comment_id": None, "ep_head": None,
        }
        entered = False
        sig_changed = False
        episode = int(prev["episode"])
        ep_head = prev["ep_head"]
        if gate_status == "blocked":
            if prev["status"] != "blocked":
                episode = int(prev["episode"]) + 1
                ep_head = head_sha
                entered = True
            else:
                sig_changed = prev["sig"] != ci_signature
        should_notify = gate_status == "blocked" and int(prev["notified"]) < episode
        self.gate[issue_key] = {
            "status": gate_status, "episode": episode, "notified": int(prev["notified"]),
            "sig": ci_signature, "comment_id": prev["comment_id"], "ep_head": ep_head,
        }
        state = PrReviewGateState(
            issue_key=issue_key, repo=repo, pr_number=pr_number, current_head_sha=head_sha,
            gate_status=gate_status, blocked_episode=episode, episode_start_head_sha=ep_head,
            last_notified_episode=int(prev["notified"]), last_notice_comment_id=prev["comment_id"],
            last_ci_signature=ci_signature, first_pending_at=None, last_checked_at=None,
            updated_at=_now_iso(),
        )
        return PrReviewGateTransition(
            state=state, entered_blocked_episode=entered, should_notify=should_notify,
            episode=episode, signature_changed=sig_changed,
        )

    def mark_pr_review_gate_notified(self, issue_key, *, episode, comment_id) -> None:
        g = self.gate.setdefault(
            issue_key,
            {"status": "unknown", "episode": episode, "notified": 0, "sig": None, "comment_id": None, "ep_head": None},
        )
        g["notified"] = episode
        g["comment_id"] = comment_id

    def get_pr_review_gate_state(self, issue_key):
        return None

class _FakeSandbox:
    natives_cache = None

    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def ensure_workspace(self, **kwargs: object) -> SimpleNamespace:
        self.kwargs = kwargs
        return SimpleNamespace(branch="review", session_dir=Path("/tmp/session"), review_head_sha=kwargs.get("pr_head_sha"))

    def remove_superseded_pr_review_workspaces(self, *, repo: str, number: int, keep_head_sha: str) -> int:
        return 0


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

def _bot_review(state: str, commit_id: str, *, body: str = "review body") -> PullRequestReviewInfo:
    return PullRequestReviewInfo(
        id=100,
        author="robomp-bot",
        body=body,
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
    # Transient pending deferral must not post a notice (would spam every PR).
    assert github.posted_comments == []


@pytest.mark.asyncio
async def test_review_pr_skips_when_ci_failed(settings) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("failed", total=1, failed=1))

    outcome = await _call_review(settings, github, received_at=_now_iso())

    assert outcome is not None
    assert outcome.state == "skipped"
    assert (outcome.reason or "").startswith("skip: PR CI checks failed")
    assert github.list_pr_reviews_called is False
    assert len(github.posted_comments) == 1
    assert "until all CI checks pass" in github.posted_comments[0]


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
    settings.pr_review_started_comments_enabled = True
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
    assert focus.prior_review_state == "CHANGES_REQUESTED"
    assert focus.prior_review_commit_id == OLD_HEAD_SHA
    assert focus.prior_review_id == 100
    assert focus.prior_review_submitted_at
    assert "verifying fixes" in github.posted_comments[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["APPROVED", "COMMENTED"])
async def test_review_pr_sets_verify_fixes_focus_for_old_commented_or_approved(
    settings, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    settings.pr_review_ci_gate_enabled = True
    settings.pr_review_started_comments_enabled = True
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
    focus = captured["inputs"].pr_review_focus
    assert focus.mode == "verify-fixes"
    assert focus.prior_review_state == state
    assert focus.prior_review_commit_id == OLD_HEAD_SHA
    assert "re-reviewing the changes since my last review" in github.posted_comments[0]


@pytest.mark.asyncio
async def test_review_pr_keeps_fresh_focus_for_retryable_comment(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(
        _ci("passed", total=1),
        reviews=[_bot_review("COMMENTED", OLD_HEAD_SHA, body="local backend command could not run because uv is unavailable")],
    )
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
    assert focus.prior_review_state == "CHANGES_REQUESTED"


def _seed_prior_suggestion(db, *, replacement: str | None = "return value;") -> None:
    finding: dict[str, object] = {
        "path": "src/app.py",
        "line": 10,
        "start_line": 10,
        "body": "Return the checked value.",
        "severity": "required",
        "intent": "required_change",
    }
    if replacement is not None:
        finding["suggestion"] = {"kind": "github_suggestion", "replacement": replacement}
    db.record_pr_review_posted_findings(
        issue_key="octo/widget#9",
        repo="octo/widget",
        pr_number=9,
        head_sha=OLD_HEAD_SHA,
        review_id=100,
        findings=[finding],
        posted_comments=[{"id": 456, "path": "src/app.py", "line": 10, "body": "Return the checked value."}],
    )


def _exact_delta(extra: str = "") -> str:
    return (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -10,1 +10,1 @@\n"
        "-return old_value;\n"
        "+return value;\n"
        f"{extra}"
    )


@pytest.mark.asyncio
async def test_review_pr_fast_path_approves_exact_prior_suggestion(
    settings, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.pr_review_ci_gate_enabled = True
    settings.pr_review_terminal_events = True
    _seed_prior_suggestion(db)
    github = _FakeGitHub(_ci("passed", total=1), reviews=[_bot_review("CHANGES_REQUESTED", OLD_HEAD_SHA)])
    calls: list[str] = []

    async def _run_task(**kwargs: object) -> None:
        calls.append(str(kwargs["task_kind"]))

    def _run_git(workspace, cmd, *, timeout):
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=_exact_delta(), stderr="")

    monkeypatch.setattr(tasks, "run_task", _run_task)
    monkeypatch.setattr(tasks, "_run_workspace_git", _run_git)

    outcome = await tasks.review_pr(
        settings=settings,
        db=db,
        github=github,  # type: ignore[arg-type]
        sandbox=_FakeSandbox(),  # type: ignore[arg-type]
        git_transport=_FakeGitTransport(),  # type: ignore[arg-type]
        payload=_payload(),
        delivery_id="delivery",
        received_at=_now_iso(),
    )

    assert outcome is not None
    assert outcome.state == "done"
    assert outcome.reason == "accepted prior GitHub suggestions exactly; skipped verify-fixes agent"
    assert calls == []
    assert github.posted_comments == []
    assert len(github.submitted_reviews) == 1
    assert github.submitted_reviews[0]["event"] == "APPROVE"
    assert github.submitted_reviews[0]["commit_id"] == HEAD_SHA
    assert "applied exactly" in str(github.submitted_reviews[0]["body"])
    assert db.has_completed_pr_review("octo/widget", 9, HEAD_SHA)


@pytest.mark.asyncio
async def test_review_pr_fast_path_falls_back_on_extra_delta(settings, db, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.pr_review_ci_gate_enabled = True
    settings.pr_review_started_comments_enabled = True
    settings.pr_review_terminal_events = True
    _seed_prior_suggestion(db)
    github = _FakeGitHub(_ci("passed", total=1), reviews=[_bot_review("CHANGES_REQUESTED", OLD_HEAD_SHA)])
    captured = {}

    async def _run_task(**kwargs: object) -> None:
        captured["inputs"] = kwargs["inputs"]

    def _run_git(workspace, cmd, *, timeout):
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        extra = (
            "diff --git a/src/other.py b/src/other.py\n"
            "--- a/src/other.py\n"
            "+++ b/src/other.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )
        return SimpleNamespace(returncode=0, stdout=_exact_delta(extra), stderr="")

    monkeypatch.setattr(tasks, "run_task", _run_task)
    monkeypatch.setattr(tasks, "_run_workspace_git", _run_git)

    outcome = await tasks.review_pr(
        settings=settings,
        db=db,
        github=github,  # type: ignore[arg-type]
        sandbox=_FakeSandbox(),  # type: ignore[arg-type]
        git_transport=_FakeGitTransport(),  # type: ignore[arg-type]
        payload=_payload(),
        delivery_id="delivery",
        received_at=_now_iso(),
    )

    assert outcome is None
    assert github.submitted_reviews == []
    assert len(github.posted_comments) == 1
    assert "verifying fixes" in github.posted_comments[0]
    assert captured["inputs"].pr_review_focus.mode == "verify-fixes"


@pytest.mark.asyncio
async def test_review_pr_fast_path_comments_when_terminal_events_disabled(
    settings, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.pr_review_ci_gate_enabled = True
    settings.pr_review_terminal_events = False
    _seed_prior_suggestion(db)
    github = _FakeGitHub(_ci("passed", total=1), reviews=[_bot_review("CHANGES_REQUESTED", OLD_HEAD_SHA)])
    calls: list[str] = []

    async def _run_task(**kwargs: object) -> None:
        calls.append(str(kwargs["task_kind"]))

    def _run_git(workspace, cmd, *, timeout):
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=_exact_delta(), stderr="")

    monkeypatch.setattr(tasks, "run_task", _run_task)
    monkeypatch.setattr(tasks, "_run_workspace_git", _run_git)

    outcome = await tasks.review_pr(
        settings=settings,
        db=db,
        github=github,  # type: ignore[arg-type]
        sandbox=_FakeSandbox(),  # type: ignore[arg-type]
        git_transport=_FakeGitTransport(),  # type: ignore[arg-type]
        payload=_payload(),
        delivery_id="delivery",
        received_at=_now_iso(),
    )

    assert outcome is not None
    assert outcome.state == "done"
    assert calls == []
    assert github.posted_comments == []
    assert github.submitted_reviews[0]["event"] == "COMMENT"
    assert "non-blocking status comment" in str(github.submitted_reviews[0]["body"])
    assert db.has_completed_pr_review("octo/widget", 9, HEAD_SHA)


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
    assert len(github.posted_comments) == 1
    assert "until all CI checks pass" in github.posted_comments[0]


@pytest.mark.asyncio
async def test_review_pr_does_not_fast_path_for_commented_prior(
    settings, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even with a recorded blocking suggestion, a prior COMMENT (non-blocking) must not trigger
    # the accepted-suggestion fast path; it runs an incremental verify-fixes review instead.
    settings.pr_review_ci_gate_enabled = True
    settings.pr_review_terminal_events = True
    _seed_prior_suggestion(db)
    github = _FakeGitHub(_ci("passed", total=1), reviews=[_bot_review("COMMENTED", OLD_HEAD_SHA)])
    captured = {}

    async def _run_task(**kwargs: object) -> None:
        captured["inputs"] = kwargs["inputs"]

    monkeypatch.setattr(tasks, "run_task", _run_task)

    outcome = await tasks.review_pr(
        settings=settings,
        db=db,
        github=github,  # type: ignore[arg-type]
        sandbox=_FakeSandbox(),  # type: ignore[arg-type]
        git_transport=_FakeGitTransport(),  # type: ignore[arg-type]
        payload=_payload(),
        delivery_id="delivery",
        received_at=_now_iso(),
    )

    assert outcome is None
    assert github.submitted_reviews == []
    assert captured["inputs"].pr_review_focus.mode == "verify-fixes"
    assert captured["inputs"].pr_review_focus.prior_review_state == "COMMENTED"


class _GateGitHub:
    """Minimal backend recording gate notice posts/edits for episode tests."""

    def __init__(self) -> None:
        self.posted: list[tuple[int, str]] = []
        self.updated: list[tuple[int, str]] = []

    async def post_comment(self, repo: str, number: int, body: str) -> CommentInfo:
        self.posted.append((number, body))
        return CommentInfo(id=500 + len(self.posted), author="robomp-bot", body=body, created_at=_now_iso())

    async def update_comment(self, repo: str, comment_id: int, body: str) -> CommentInfo:
        self.updated.append((comment_id, body))
        return CommentInfo(id=comment_id, author="robomp-bot", body=body, created_at=_now_iso())


def _ci_for(head: str, state: str, *, total: int = 3, pending: int = 0, failed: int = 0) -> PullRequestCiStatusInfo:
    return PullRequestCiStatusInfo(
        head_sha=head, state=state,  # type: ignore[arg-type]
        total_count=total, pending_count=pending, failed_count=failed,
    )


async def _gate(settings, db, gh, head: str, ci: PullRequestCiStatusInfo):
    return await tasks._apply_pr_review_ci_gate(
        settings=settings, db=db, github=gh, key="octo/widget#9",
        repo_full="octo/widget", pr_number=9, head_sha=head, ci=ci, received_at=_now_iso(),
    )


@pytest.mark.asyncio
async def test_ci_gate_notice_once_per_blocking_episode(settings, db) -> None:
    settings.pr_review_ci_gate_enabled = True
    gh = _GateGitHub()
    a, b, c, d = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
    # First red head opens episode 1 -> one notice.
    out = await _gate(settings, db, gh, a, _ci_for(a, "failed", failed=1, pending=1))
    assert out is not None and out.state == "skipped"
    assert len(gh.posted) == 1
    # A different red head in the SAME episode must NOT post again (was the bug:
    # one "won't review" comment per failing push).
    out = await _gate(settings, db, gh, b, _ci_for(b, "failed", failed=2))
    assert out is not None and out.state == "skipped"
    assert len(gh.posted) == 1
    # Green head proceeds, posts nothing, and ends the episode.
    out = await _gate(settings, db, gh, c, _ci_for(c, "passed"))
    assert out is None
    assert len(gh.posted) == 1
    assert db.get_pr_review_gate_state("octo/widget#9").gate_status == "passed"
    # A new red head AFTER passing opens episode 2 -> exactly one new notice.
    out = await _gate(settings, db, gh, d, _ci_for(d, "failed", failed=1))
    assert out is not None and out.state == "skipped"
    assert len(gh.posted) == 2
    assert db.get_pr_review_gate_state("octo/widget#9").blocked_episode == 2


@pytest.mark.asyncio
async def test_ci_gate_pending_posts_nothing(settings, db) -> None:
    settings.pr_review_ci_gate_enabled = True
    gh = _GateGitHub()
    h = "e" * 40
    out = await _gate(settings, db, gh, h, _ci_for(h, "pending", total=2, pending=2))
    assert out is not None and out.state == "queued"
    assert gh.posted == []
    assert db.get_pr_review_gate_state("octo/widget#9").gate_status == "pending"


@pytest.mark.asyncio
async def test_ci_gate_sticky_edit_on_signature_change(settings, db) -> None:
    settings.pr_review_ci_gate_enabled = True
    settings.pr_review_gate_sticky_comment_enabled = True
    gh = _GateGitHub()
    h = "f" * 40
    await _gate(settings, db, gh, h, _ci_for(h, "failed", failed=1, pending=2))
    assert len(gh.posted) == 1 and gh.updated == []
    # Same episode, counts changed -> edit the one comment in place, no new post.
    await _gate(settings, db, gh, h, _ci_for(h, "failed", failed=3, pending=0))
    assert len(gh.posted) == 1 and len(gh.updated) == 1
    assert gh.updated[0][0] == 501  # comment id from the original post
    # Identical counts -> no redundant edit.
    await _gate(settings, db, gh, h, _ci_for(h, "failed", failed=3, pending=0))
    assert len(gh.updated) == 1


@pytest.mark.asyncio
async def test_ci_gate_sticky_disabled_does_not_edit(settings, db) -> None:
    settings.pr_review_ci_gate_enabled = True
    settings.pr_review_gate_sticky_comment_enabled = False
    gh = _GateGitHub()
    h = "9" * 40
    await _gate(settings, db, gh, h, _ci_for(h, "failed", failed=1))
    await _gate(settings, db, gh, h, _ci_for(h, "failed", failed=5))
    assert len(gh.posted) == 1
    assert gh.updated == []


def _payload_with_head(sha: str) -> dict[str, object]:
    return {"repository": {"full_name": "octo/widget"}, "pull_request": {"number": 9, "head": {"sha": sha}}}


@pytest.mark.asyncio
async def test_review_pr_skips_superseded_head(settings) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("passed", total=1))  # live head == HEAD_SHA
    outcome = await tasks.review_pr(
        settings=settings,
        db=_FakeDb(),  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        sandbox=_FakeSandbox(),  # type: ignore[arg-type]
        git_transport=_FakeGitTransport(),  # type: ignore[arg-type]
        payload=_payload_with_head(OLD_HEAD_SHA),
        delivery_id="delivery",
        received_at=_now_iso(),
    )
    assert outcome is not None and outcome.state == "skipped"
    assert "superseded" in (outcome.reason or "")
    assert github.posted_comments == []
    assert github.list_pr_files_called is False


@pytest.mark.asyncio
async def test_review_pr_no_started_comment_by_default(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.pr_review_ci_gate_enabled = True
    assert settings.pr_review_started_comments_enabled is False
    github = _FakeGitHub(_ci("passed", total=1))

    async def _run_task(**kwargs: object) -> None:
        return None

    monkeypatch.setattr(tasks, "run_task", _run_task)
    outcome = await _call_review(settings, github, received_at=_now_iso())
    assert outcome is None
    assert github.posted_comments == []


_PROBE_REVIEW_DELIVERY = f"ci-probe-review-octo__widget-9-{HEAD_SHA}"


@pytest.mark.asyncio
async def test_probe_enqueues_review_when_ci_green(settings, db) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("passed", total=1))
    out = await tasks.probe_pr_review_ci(
        settings=settings, db=db, github=github,  # type: ignore[arg-type]
        payload=_payload(), delivery_id="probe-1", received_at=_now_iso(),
    )
    assert out is not None and out.state == "done"
    ev = db.get_event(_PROBE_REVIEW_DELIVERY)
    assert ev is not None and ev.task == "review_pr" and ev.state == "queued"
    assert github.posted_comments == []


@pytest.mark.asyncio
async def test_probe_blocks_and_notifies_without_enqueue_when_ci_red(settings, db) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("failed", total=2, failed=1))
    out = await tasks.probe_pr_review_ci(
        settings=settings, db=db, github=github,  # type: ignore[arg-type]
        payload=_payload(), delivery_id="probe-1", received_at=_now_iso(),
    )
    assert out is not None and out.state == "skipped"
    assert db.get_event(_PROBE_REVIEW_DELIVERY) is None
    assert len(github.posted_comments) == 1
    assert "until all CI checks pass" in github.posted_comments[0]


@pytest.mark.asyncio
async def test_probe_defers_without_enqueue_when_ci_pending(settings, db) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("pending", total=2, pending=2))
    out = await tasks.probe_pr_review_ci(
        settings=settings, db=db, github=github,  # type: ignore[arg-type]
        payload=_payload(), delivery_id="probe-1", received_at=_now_iso(),
    )
    assert out is not None and out.state == "queued"
    assert db.get_event(_PROBE_REVIEW_DELIVERY) is None
    assert github.posted_comments == []


@pytest.mark.asyncio
async def test_probe_skips_superseded_head(settings, db) -> None:
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("passed", total=1))  # live head == HEAD_SHA
    out = await tasks.probe_pr_review_ci(
        settings=settings, db=db, github=github,  # type: ignore[arg-type]
        payload=_payload_with_head(OLD_HEAD_SHA), delivery_id="probe-1", received_at=_now_iso(),
    )
    assert out is not None and out.state == "skipped"
    assert "superseded" in (out.reason or "")
    assert db.get_event(_PROBE_REVIEW_DELIVERY) is None


@pytest.mark.asyncio
async def test_probe_skips_already_reviewed_head(settings, db) -> None:
    settings.pr_review_ci_gate_enabled = True
    db.record_pr_review_completed_review(
        issue_key="octo/widget#9", repo="octo/widget", pr_number=9,
        head_sha=HEAD_SHA, github_review_id=1, event="APPROVE",
    )
    github = _FakeGitHub(_ci("passed", total=1))
    out = await tasks.probe_pr_review_ci(
        settings=settings, db=db, github=github,  # type: ignore[arg-type]
        payload=_payload(), delivery_id="probe-1", received_at=_now_iso(),
    )
    assert out is not None and out.state == "skipped"
    assert "already submitted" in (out.reason or "")
    assert db.get_event(_PROBE_REVIEW_DELIVERY) is None


def _check_suite_payload(head: str = HEAD_SHA) -> dict[str, object]:
    # check_suite webhook shape: no pull_request.number; head_sha + pull_requests.
    return {"repository": {"full_name": "octo/widget"}, "check_suite": {"head_sha": head, "pull_requests": [{"number": 9}]}}


@pytest.mark.asyncio
async def test_probe_resolves_pr_from_event_issue_key(settings, db) -> None:
    # A check_suite payload carries no pull_request.number; the probe must use the
    # router-resolved issue_key to find the PR (else it would skip).
    settings.pr_review_ci_gate_enabled = True
    github = _FakeGitHub(_ci("passed", total=1))
    out = await tasks.probe_pr_review_ci(
        settings=settings, db=db, github=github,  # type: ignore[arg-type]
        payload=_check_suite_payload(), delivery_id="cs-1", received_at=_now_iso(),
        event_issue_key="octo/widget#9",
    )
    assert out is not None and out.state == "done"
    assert db.get_event(_PROBE_REVIEW_DELIVERY) is not None


@pytest.mark.asyncio
async def test_probe_debounces_repeat_check_for_same_head(settings, db) -> None:
    settings.pr_review_ci_gate_enabled = True
    settings.pr_review_ci_debounce_seconds = 999.0
    github = _FakeGitHub(_ci("failed", total=2, failed=1))
    payload = _check_suite_payload()
    out1 = await tasks.probe_pr_review_ci(
        settings=settings, db=db, github=github,  # type: ignore[arg-type]
        payload=payload, delivery_id="cs-1", received_at=_now_iso(), event_issue_key="octo/widget#9",
    )
    assert out1 is not None and out1.state == "skipped"
    assert len(github.posted_comments) == 1  # episode notice on first probe
    # Immediate repeat for the same head is debounced before any GitHub fetch.
    github2 = _FakeGitHub(_ci("failed", total=2, failed=1))
    out2 = await tasks.probe_pr_review_ci(
        settings=settings, db=db, github=github2,  # type: ignore[arg-type]
        payload=payload, delivery_id="cs-2", received_at=_now_iso(), event_issue_key="octo/widget#9",
    )
    assert out2 is not None and out2.state == "skipped"
    assert "debounced" in (out2.reason or "")
    assert github2.posted_comments == []


@pytest.mark.asyncio
async def test_probe_not_debounced_for_new_head(settings, db) -> None:
    settings.pr_review_ci_gate_enabled = True
    settings.pr_review_ci_debounce_seconds = 999.0
    github = _FakeGitHub(_ci("failed", total=2, failed=1))
    # First probe records gate-state for the live head (HEAD_SHA), blocked.
    await tasks.probe_pr_review_ci(
        settings=settings, db=db, github=github,  # type: ignore[arg-type]
        payload=_check_suite_payload(head=HEAD_SHA), delivery_id="cs-1",
        received_at=_now_iso(), event_issue_key="octo/widget#9",
    )
    # A check event for a DIFFERENT head than the recorded one must NOT be
    # debounced — it falls through, re-fetches the live PR, and re-evaluates.
    github2 = _FakeGitHub(_ci("passed", total=1))
    out = await tasks.probe_pr_review_ci(
        settings=settings, db=db, github=github2,  # type: ignore[arg-type]
        payload=_check_suite_payload(head="dead" + "0" * 36), delivery_id="cs-2",
        received_at=_now_iso(), event_issue_key="octo/widget#9",
    )
    assert out is not None and out.state == "done"
    assert db.get_event(_PROBE_REVIEW_DELIVERY) is not None
