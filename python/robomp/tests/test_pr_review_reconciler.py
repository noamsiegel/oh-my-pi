from __future__ import annotations

from pathlib import Path

import pytest

from robomp.db import Database
from robomp.github_types import PullRequestInfo
from robomp.pr_review_reconciler import PrReviewLabelReconciler, reconciled_delivery_id


class _FakeGitHub:
    def __init__(self, prs: list[PullRequestInfo]) -> None:
        self.prs = prs

    async def list_open_pull_requests(self, repo: str, *, limit: int = 30) -> list[PullRequestInfo]:
        assert repo == "octo/widget"
        return self.prs[:limit]


class _FakePool:
    def __init__(self) -> None:
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "robomp.sqlite")


def _pr(
    *,
    labels: tuple[str, ...] = ("robo-review",),
    head_sha: str = "abc123",
    updated_at: str = "",
) -> PullRequestInfo:
    return PullRequestInfo(
        repo="octo/widget",
        number=9,
        html_url="https://github.com/octo/widget/pull/9",
        head_ref="feature",
        base_ref="main",
        state="open",
        author="alice",
        head_sha=head_sha,
        labels=labels,
        updated_at=updated_at,
    )


def _reconciler(db: Database, pool: _FakePool, prs: list[PullRequestInfo]) -> PrReviewLabelReconciler:
    return PrReviewLabelReconciler(
        db=db,
        github=_FakeGitHub(prs),  # type: ignore[arg-type]
        pool=pool,  # type: ignore[arg-type]
        repos=frozenset({"octo/widget"}),
        label_allowlist=frozenset({"robo-review"}),
        bot_login="robomp-bot",
        interval_seconds=60,
        limit_per_repo=30,
    )


@pytest.mark.asyncio
async def test_reconciler_queues_labeled_pr_missing_webhook(tmp_path: Path) -> None:
    db = _db(tmp_path)
    pool = _FakePool()
    reconciler = _reconciler(db, pool, [_pr()])

    assert await reconciler.reconcile_once() == 1
    assert pool.wakes == 1
    row = db.get_event(reconciled_delivery_id("octo/widget", 9, "abc123"))
    assert row is not None
    assert row.state == "queued"
    assert row.task == "review_pr"
    assert row.issue_key == "octo/widget#9"
    assert row.payload["pull_request"]["number"] == 9

    decisions = db._conn.execute(
        "SELECT decision, reason, event_delivery_id FROM pr_review_reconciler_decisions WHERE repo=? AND pr_number=?",
        ("octo/widget", 9),
    ).fetchall()
    assert [(row["decision"], row["reason"], row["event_delivery_id"]) for row in decisions] == [
        ("queued", "missing_webhook_or_unreviewed_head", reconciled_delivery_id("octo/widget", 9, "abc123"))
    ]

@pytest.mark.asyncio
async def test_reconciler_uses_durable_cursor_after_initial_backfill(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.set_pr_review_reconciler_cursor("octo/widget", "2026-06-11T10:00:00Z")
    pool = _FakePool()
    reconciler = _reconciler(db, pool, [_pr(updated_at="2026-06-11T09:00:00Z")])

    assert await reconciler.reconcile_once() == 0
    assert pool.wakes == 0


@pytest.mark.asyncio
async def test_reconciler_skips_completed_head(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.record_pr_review_completed_review(
        issue_key="octo/widget#9",
        repo="octo/widget",
        pr_number=9,
        head_sha="abc123",
        github_review_id=123,
        event="COMMENT",
    )
    pool = _FakePool()
    reconciler = _reconciler(db, pool, [_pr()])

    assert await reconciler.reconcile_once() == 0
    assert pool.wakes == 0


@pytest.mark.asyncio
async def test_reconciler_skips_active_event(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.record_event(
        delivery_id="existing",
        event_type="pull_request",
        repo="octo/widget",
        issue_key="octo/widget#9",
        payload={"pull_request": {"number": 9}},
        state="queued",
        task="review_pr",
        route_reason="pull_request.labeled",
    )
    pool = _FakePool()
    reconciler = _reconciler(db, pool, [_pr()])

    assert await reconciler.reconcile_once() == 0
    assert pool.wakes == 0
