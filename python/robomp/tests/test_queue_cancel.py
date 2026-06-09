"""Cancellation primitives on WorkerPool.

These tests stay at the public-ish surface of `WorkerPool` — they exercise the
hook registration contextvar that workers use and verify the dispatcher marks
cancelled events as failed with the documented marker. They do NOT spin up a
real omp subprocess; that's covered by the integration smoke test.
"""

from __future__ import annotations

import asyncio

import pytest

from robomp.cancellation import (
    clear_current_event,
    register_cancel_hook,
    set_current_event,
    unregister_cancel_hook,
)
from robomp.config import Settings
from robomp.db import Database, EventRow
from robomp.git_ops import GitCommandError
from robomp.queue import WorkerPool
from robomp.slot_pool import SlotPool
from tests.fakes import (
    RecordingGitHub as _RecordingGitHub,
)
from tests.fakes import (
    RecordingSandbox as _StubSandbox,
)
from tests.fakes import (
    StubGitTransport as _StubGitTransport,
)
from tests.fakes import (
    event_row_factory as _row,
)
from tests.fakes import (
    make_pool as _make_pool,
)


def _make_pool_with_github(settings: Settings, db: Database, github: object) -> WorkerPool:
    return _make_pool(settings, db, github=github)


@pytest.mark.asyncio
async def test_cancel_fires_hook_armed_by_worker(settings: Settings, db: Database) -> None:
    """A worker that armed a hook gets it invoked when cancel_event runs."""
    pool = _make_pool(settings, db)
    row = _row()

    fired = asyncio.Event()

    async def fake_worker() -> None:
        # Mimic _run_event entering its contextvar scope: the helpers below are
        # what worker.py invokes from inside the asyncio.to_thread call.
        token = set_current_event(pool, row.delivery_id)
        try:
            await asyncio.to_thread(register_cancel_hook, fired.set)
            # Park until somebody fires the hook.
            await fired.wait()
        finally:
            await asyncio.to_thread(unregister_cancel_hook)
            clear_current_event(token)

    worker = asyncio.create_task(fake_worker())
    # Give the worker a tick to register.
    for _ in range(20):
        await asyncio.sleep(0)
        if row.delivery_id in pool._cancel_hooks:  # noqa: SLF001 — test inspecting state
            break
    assert row.delivery_id in pool._cancel_hooks  # noqa: SLF001

    assert await pool.cancel_event(row.delivery_id) is True
    await asyncio.wait_for(worker, timeout=1.0)
    assert row.delivery_id in pool._cancelled  # noqa: SLF001
    # Hook is consumed.
    assert row.delivery_id not in pool._cancel_hooks  # noqa: SLF001


@pytest.mark.asyncio
async def test_cancel_before_arm_fires_immediately(settings: Settings, db: Database) -> None:
    """Cancelling before the worker arms must still terminate it on register."""
    pool = _make_pool(settings, db)
    row = _row("d2")

    # Cancel is requested before any worker has armed a hook.
    assert await pool.cancel_event(row.delivery_id) is False
    assert row.delivery_id in pool._cancelled  # noqa: SLF001

    # When the worker eventually registers, the hook must fire synchronously.
    calls: list[int] = []
    token = set_current_event(pool, row.delivery_id)
    try:
        register_cancel_hook(lambda: calls.append(1))
    finally:
        clear_current_event(token)

    assert calls == [1]
    # Late-armed hook is NOT retained; cancel state is one-shot.
    assert row.delivery_id not in pool._cancel_hooks  # noqa: SLF001


@pytest.mark.asyncio
async def test_dispatch_marks_cancelled_event_failed_with_marker(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatch that observed cancellation marks the row failed + 'cancelled by operator'."""
    pool = _make_pool(settings, db)

    db.record_event(
        delivery_id="d3",
        event_type="issues",
        repo="octo/widget",
        issue_key="octo/widget#1",
        payload={"action": "opened"},
        state="running",
    )
    row = _row("d3")

    async def fake_dispatch(self: WorkerPool, r: EventRow, *, slot_uid: int | None = None) -> None:
        # Simulate cancellation hitting mid-task and the omp subprocess raising.
        await pool.cancel_event(r.delivery_id)
        raise RuntimeError("subprocess died")

    monkeypatch.setattr(WorkerPool, "_dispatch", fake_dispatch)
    await pool._run_event(row)  # noqa: SLF001 — testing the dispatcher branch directly

    stored = db.get_event("d3")
    assert stored is not None
    assert stored.state == "failed"
    assert stored.last_error == "cancelled by operator"
    # State is cleared for future events.
    assert row.delivery_id not in pool._cancelled  # noqa: SLF001
    assert row.delivery_id not in pool._cancel_hooks  # noqa: SLF001


@pytest.mark.asyncio
async def test_non_cancelled_failure_keeps_real_traceback(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A garden-variety dispatch failure still records the traceback path."""
    pool = _make_pool(settings, db)
    db.record_event(
        delivery_id="d4",
        event_type="issues",
        repo="octo/widget",
        issue_key="octo/widget#1",
        payload={"action": "opened"},
        state="running",
    )
    row = _row("d4")

    async def fake_dispatch(self: WorkerPool, r: EventRow, *, slot_uid: int | None = None) -> None:
        raise ValueError("boom 42")

    monkeypatch.setattr(WorkerPool, "_dispatch", fake_dispatch)
    await pool._run_event(row)  # noqa: SLF001

    stored = db.get_event("d4")
    assert stored is not None
    assert stored.state == "failed"
    assert stored.last_error is not None
    assert "boom 42" in stored.last_error
    assert "cancelled by operator" not in stored.last_error



@pytest.mark.asyncio
async def test_pull_request_workspace_timeout_requeues_once_before_comment(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = _RecordingGitHub()
    pool = _make_pool_with_github(settings, db, github)
    monkeypatch.setattr("robomp.queue.PR_WORKSPACE_RETRY_DELAY_SECONDS", 0.0)
    db.record_event(
        delivery_id="d-pr-retry",
        event_type="pull_request",
        repo="octo/widget",
        issue_key="octo/widget#9",
        payload={"action": "labeled", "pull_request": {"number": 9}},
    )
    first = db.claim_next_event()
    assert first is not None
    assert first.attempts == 1
    calls = 0

    async def fake_dispatch(self: WorkerPool, r: EventRow, *, slot_uid: int | None = None) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GitCommandError(["git", "fetch"], 124, "", "git timed out after 295s")

    monkeypatch.setattr(WorkerPool, "_dispatch", fake_dispatch)
    await pool._run_event(first)  # noqa: SLF001

    stored = db.get_event("d-pr-retry")
    assert stored is not None
    assert stored.state == "queued"
    assert stored.last_error is not None
    assert "retrying workspace preparation after transient git failure" in stored.last_error
    assert github.comments == []

    second = db.claim_next_event()
    assert second is not None
    assert second.attempts == 2
    await pool._run_event(second)  # noqa: SLF001
    stored = db.get_event("d-pr-retry")
    assert stored is not None
    assert stored.state == "done"
    assert github.comments == []


@pytest.mark.asyncio
async def test_pull_request_policy_skip_marks_event_skipped(settings: Settings, db: Database) -> None:
    from robomp.github_types import (
        IssueInfo,
        PullRequestInfo,
        RepoInfo,
    )

    settings.pr_review_label_allowlist_raw = "robo-review"

    class MissingLabelGitHub(_RecordingGitHub):
        async def get_repo(self, repo_full: str) -> RepoInfo:
            return RepoInfo(repo_full, "main", "https://github.com/octo/widget.git", False)

        async def get_issue(self, repo_full: str, number: int) -> IssueInfo:
            return IssueInfo(repo_full, number, "Fix parser", "body", "open", "alice", ("backend",), True)

        async def get_pull_request(self, repo_full: str, number: int) -> PullRequestInfo:
            return PullRequestInfo(repo_full, number, "https://github.com/octo/widget/pull/9", "alice/fix", "main", "open", "alice", "alice/widget")

    github = MissingLabelGitHub()
    pool = _make_pool_with_github(settings, db, github)
    db.record_event(
        delivery_id="d-pr-skip",
        event_type="pull_request",
        repo="octo/widget",
        issue_key="octo/widget#9",
        payload={"action": "labeled", "pull_request": {"number": 9}, "repository": {"full_name": "octo/widget"}},
    )
    row = db.claim_next_event()
    assert row is not None

    await pool._run_event(row)  # noqa: SLF001

    stored = db.get_event("d-pr-skip")
    assert stored is not None
    assert stored.state == "skipped"
    assert stored.last_error == "skip: PR missing review trigger label"
    assert github.comments == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_review_pr_github_fetch_error_requeues_then_fails_without_retry_comment(
    settings: Settings, db: Database, status: int
) -> None:
    from robomp.github_types import GitHubError

    class FetchFailingGitHub(_RecordingGitHub):
        async def get_repo(self, repo_full: str) -> object:
            raise GitHubError(status, "upstream unavailable", retry_after=0.0)

    github = FetchFailingGitHub()
    pool = _make_pool_with_github(settings, db, github)
    db.record_event(
        delivery_id=f"d-pr-fetch-{status}",
        event_type="pull_request",
        repo="octo/widget",
        issue_key="octo/widget#9",
        payload={"action": "opened", "pull_request": {"number": 9}, "repository": {"full_name": "octo/widget"}},
    )
    first = db.claim_next_event()
    assert first is not None
    assert first.attempts == 1

    await pool._run_event(first)  # noqa: SLF001

    stored = db.get_event(f"d-pr-fetch-{status}")
    assert stored is not None
    assert stored.state == "queued"
    assert stored.last_error == f"GitHub fetch failed: GitHub {status}: upstream unavailable"
    assert github.comments == []

    second = db.claim_next_event()
    assert second is not None
    assert second.attempts == 2

    await pool._run_event(second)  # noqa: SLF001

    stored = db.get_event(f"d-pr-fetch-{status}")
    assert stored is not None
    assert stored.state == "failed"
    assert stored.last_error == f"GitHub fetch failed: GitHub {status}: upstream unavailable"
    assert len(github.comments) == 1
    assert f"- Delivery: `d-pr-fetch-{status}`" in github.comments[0][2]

@pytest.mark.asyncio
async def test_pull_request_failure_posts_loud_github_comment(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = _RecordingGitHub()
    pool = _make_pool_with_github(settings, db, github)
    db.record_event(
        delivery_id="d-pr-fail",
        event_type="pull_request",
        repo="octo/widget",
        issue_key="octo/widget#9",
        payload={"action": "labeled", "pull_request": {"number": 9}},
        state="running",
    )
    row = EventRow(
        delivery_id="d-pr-fail",
        event_type="pull_request",
        repo="octo/widget",
        issue_key="octo/widget#9",
        payload={"action": "labeled", "pull_request": {"number": 9}},
        received_at="2026-01-01T00:00:00Z",
        state="running",
        attempts=1,
        last_error=None,
    )

    async def fake_dispatch(self: WorkerPool, r: EventRow, *, slot_uid: int | None = None) -> None:
        raise RuntimeError("git fetch timed out after 120s")

    monkeypatch.setattr(WorkerPool, "_dispatch", fake_dispatch)
    await pool._run_event(row)  # noqa: SLF001

    stored = db.get_event("d-pr-fail")
    assert stored is not None
    assert stored.state == "failed"
    assert github.comments == [
        (
            "octo/widget",
            9,
            "Robo-MS failed before it could finish this PR review.\n\n"
            "- Delivery: `d-pr-fail`\n"
            "- Failure: `git fetch timed out after 120s`\n\n"
            "No review was submitted. Fix the infrastructure failure, then re-trigger by removing "
            "and re-adding the review trigger label.",
        )
    ]
    rows = db._conn.execute(
        "SELECT tool, error, result_json FROM tool_calls WHERE issue_key = ?",
        ("octo/widget#9",),
    ).fetchall()
    assert rows[0]["tool"] == "post_pr_review_failed_comment:d-pr-fail"
    assert rows[0]["error"] is None


@pytest.mark.asyncio
async def test_run_event_marks_failed_when_not_shutting_down(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `_shutting_down` is False, a dispatch failure still marks the row failed."""
    pool = _make_pool(settings, db)
    assert pool._shutting_down is False  # noqa: SLF001
    db.record_event(
        delivery_id="d5",
        event_type="issues",
        repo="octo/widget",
        issue_key="octo/widget#1",
        payload={"action": "opened"},
        state="running",
    )
    row = _row("d5")

    async def fake_dispatch(self: WorkerPool, r: EventRow, *, slot_uid: int | None = None) -> None:
        raise RuntimeError("regular failure")

    monkeypatch.setattr(WorkerPool, "_dispatch", fake_dispatch)
    await pool._run_event(row)  # noqa: SLF001

    stored = db.get_event("d5")
    assert stored is not None
    assert stored.state == "failed"
    assert stored.last_error is not None
    assert "regular failure" in stored.last_error


@pytest.mark.asyncio
async def test_cancel_unknown_delivery_returns_false(settings: Settings, db: Database) -> None:
    """Cancelling an unknown delivery is a no-op that returns False."""
    pool = _make_pool(settings, db)
    assert await pool.cancel_event("never-existed") is False
    # The set still records the request — a later register would fire — but
    # since no worker is armed, the cancel is harmless.
    assert "never-existed" in pool._cancelled  # noqa: SLF001


@pytest.mark.asyncio
async def test_start_reaps_configured_slot_uids(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr("robomp.queue._reap_slot", lambda uid: calls.append(uid))
    pool = WorkerPool(
        settings=settings,
        db=db,
        github=_RecordingGitHub(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        git_transport=_StubGitTransport(),  # type: ignore[arg-type]
        slot_pool=SlotPool([2001, 2002]),
    )

    await pool.start()
    try:
        assert sorted(calls) == [2001, 2002]
    finally:
        await pool.stop(drain_timeout=0.01, kill_timeout=0.01)


@pytest.mark.asyncio
async def test_run_event_reaps_slot_before_release(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot_pool = SlotPool([2001])
    pool = WorkerPool(
        settings=settings,
        db=db,
        github=_RecordingGitHub(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        git_transport=_StubGitTransport(),  # type: ignore[arg-type]
        slot_pool=slot_pool,
    )
    db.record_event(
        delivery_id="d-slot",
        event_type="issues",
        repo="octo/widget",
        issue_key="octo/widget#1",
        payload={"action": "opened"},
        state="running",
    )
    order: list[tuple[str, int | None]] = []
    monkeypatch.setattr("robomp.queue._reap_slot", lambda uid: order.append(("reap", uid)))
    release = slot_pool.release

    def record_release(slot_uid: int | None) -> None:
        order.append(("release", slot_uid))
        release(slot_uid)

    monkeypatch.setattr(slot_pool, "release", record_release)

    async def fake_dispatch(self: WorkerPool, r: EventRow, *, slot_uid: int | None = None) -> None:
        assert r.delivery_id == "d-slot"
        assert slot_uid == 2001

    monkeypatch.setattr(WorkerPool, "_dispatch", fake_dispatch)

    await pool._run_event(_row("d-slot"))  # noqa: SLF001

    stored = db.get_event("d-slot")
    assert stored is not None
    assert stored.state == "done"
    assert order == [("reap", 2001), ("release", 2001)]
@pytest.mark.asyncio
async def test_pull_request_workspace_retry_exhaustion_posts_loud_comment(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = _RecordingGitHub()
    pool = _make_pool_with_github(settings, db, github)
    db.record_event(
        delivery_id="exhausted-retry",
        event_type="pull_request",
        repo="octo/widget",
        issue_key="octo/widget#42",
        payload={"action": "opened", "pull_request": {"number": 42}},
        state="running",
    )
    row = EventRow(
        delivery_id="exhausted-retry",
        event_type="pull_request",
        repo="octo/widget",
        issue_key="octo/widget#42",
        payload={"action": "opened", "pull_request": {"number": 42}},
        received_at="2026-05-15T10:00:00.000000Z",
        state="running",
        attempts=2,
        last_error=None,
    )

    async def fake_dispatch(self: WorkerPool, r: EventRow, *, slot_uid: int | None = None) -> None:
        raise GitCommandError(["git", "fetch"], 124, "", "git timed out after 30s")

    monkeypatch.setattr(WorkerPool, "_dispatch", fake_dispatch)
    await pool._run_event(row)  # noqa: SLF001

    stored = db.get_event("exhausted-retry")
    assert stored is not None
    assert stored.state == "failed"
    assert stored.last_error is not None
    assert "git timed out after 30s" in stored.last_error
    assert len(github.comments) == 1
    assert github.comments[0][0] == "octo/widget"
    assert github.comments[0][1] == 42
    assert "- Delivery: `exhausted-retry`" in github.comments[0][2]


@pytest.mark.asyncio
async def test_pull_request_failure_comment_idempotency_uses_side_effects(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = _RecordingGitHub()
    pool = _make_pool_with_github(settings, db, github)
    db.record_event(
        delivery_id="duplicate-test",
        event_type="pull_request",
        repo="octo/widget",
        issue_key="octo/widget#42",
        payload={"action": "opened", "pull_request": {"number": 42}},
        state="running",
    )
    assert db.reserve_side_effect("post_pr_review_failed_comment:duplicate-test")
    assert db.mark_side_effect_succeeded("post_pr_review_failed_comment:duplicate-test")
    row = EventRow(
        delivery_id="duplicate-test",
        event_type="pull_request",
        repo="octo/widget",
        issue_key="octo/widget#42",
        payload={"action": "opened", "pull_request": {"number": 42}},
        received_at="2026-05-15T10:00:00.000000Z",
        state="running",
        attempts=2,
        last_error=None,
    )

    async def fake_dispatch(self: WorkerPool, r: EventRow, *, slot_uid: int | None = None) -> None:
        raise GitCommandError(["git", "fetch"], 1, "", "git failed")

    monkeypatch.setattr(WorkerPool, "_dispatch", fake_dispatch)
    await pool._run_event(row)  # noqa: SLF001

    stored = db.get_event("duplicate-test")
    assert stored is not None
    assert stored.state == "failed"
    assert github.comments == []


@pytest.mark.asyncio
async def test_failure_comment_post_github_error_still_marks_failed_and_records_tool_call_error(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from robomp.github_types import GitHubError

    class FailingGitHub:
        async def post_comment(self, repo: str, number: int, body: str) -> object:
            raise GitHubError(500, "GitHub API error")

    pool = _make_pool_with_github(settings, db, FailingGitHub())
    db.record_event(
        delivery_id="github-post-fail",
        event_type="pull_request",
        repo="octo/widget",
        issue_key="octo/widget#42",
        payload={"action": "opened", "pull_request": {"number": 42}},
        state="running",
    )
    row = EventRow(
        delivery_id="github-post-fail",
        event_type="pull_request",
        repo="octo/widget",
        issue_key="octo/widget#42",
        payload={"action": "opened", "pull_request": {"number": 42}},
        received_at="2026-05-15T10:00:00.000000Z",
        state="running",
        attempts=2,
        last_error=None,
    )

    async def fake_dispatch(self: WorkerPool, r: EventRow, *, slot_uid: int | None = None) -> None:
        raise GitCommandError(["git", "fetch"], 1, "", "git failed")

    monkeypatch.setattr(WorkerPool, "_dispatch", fake_dispatch)
    await pool._run_event(row)  # noqa: SLF001

    stored = db.get_event("github-post-fail")
    assert stored is not None
    assert stored.state == "failed"
    assert stored.last_error is not None
    assert "git failed" in stored.last_error
    rows = db._conn.execute(  # noqa: SLF001
        "SELECT tool, error FROM tool_calls WHERE issue_key = ?",
        ("octo/widget#42",),
    ).fetchall()
    assert rows[0]["tool"] == "post_pr_review_failed_comment:github-post-fail"
    assert rows[0]["error"] is not None
    assert "GitHub API error" in rows[0]["error"]
