from __future__ import annotations

import logging
from pathlib import Path

import pytest

from robomp.db import Database
from robomp.github_types import PullRequestInfo
from robomp.pr_review_reconciler import (
    PrReviewLabelReconciler,
    reconciled_cleanup_delivery_id,
    reconciled_delivery_id,
)


class _FakeGitHub:
    def __init__(self, prs: list[PullRequestInfo], closed: list[PullRequestInfo] | None = None) -> None:
        self.by_state: dict[str, list[PullRequestInfo]] = {"open": prs, "closed": closed or []}

    async def list_pull_requests(
        self, repo: str, *, state: str = "open", limit: int = 30
    ) -> list[PullRequestInfo]:
        assert repo == "octo/widget"
        return self.by_state.get(state, [])[:limit]


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
    state: str = "open",
    merged: bool = False,
    author: str = "alice",
    author_type: str = "",
) -> PullRequestInfo:
    return PullRequestInfo(
        repo="octo/widget",
        number=9,
        html_url="https://github.com/octo/widget/pull/9",
        head_ref="feature",
        base_ref="main",
        state=state,
        author=author,
        author_type=author_type,
        head_sha=head_sha,
        labels=labels,
        updated_at=updated_at,
        merged=merged,
    )


def _reconciler(
    db: Database,
    pool: _FakePool,
    prs: list[PullRequestInfo],
    closed: list[PullRequestInfo] | None = None,
    *,
    ci_gate_enabled: bool = False,
) -> PrReviewLabelReconciler:
    return PrReviewLabelReconciler(
        db=db,
        github=_FakeGitHub(prs, closed),  # type: ignore[arg-type]
        pool=pool,  # type: ignore[arg-type]
        repos=frozenset({"octo/widget"}),
        label_allowlist=frozenset({"robo-review"}),
        bot_login="robomp-bot",
        interval_seconds=60,
        limit_per_repo=30,
        ci_gate_enabled=ci_gate_enabled,
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
async def test_reconciler_enqueues_probe_when_ci_gate_enabled(tmp_path: Path) -> None:
    db = _db(tmp_path)
    pool = _FakePool()
    reconciler = _reconciler(db, pool, [_pr()], ci_gate_enabled=True)

    assert await reconciler.reconcile_once() == 1
    row = db.get_event(reconciled_delivery_id("octo/widget", 9, "abc123"))
    assert row is not None
    assert row.state == "queued"
    # With the gate on, the reconciler enqueues the cheap CI probe, not a full
    # review_pr; the probe enqueues review_pr only once CI is green.
    assert row.task == "probe_pr_review_ci"

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


@pytest.mark.asyncio
async def test_reconciler_queues_cleanup_for_closed_pr(tmp_path: Path) -> None:
    db = _db(tmp_path)
    pool = _FakePool()
    closed_pr = _pr(state="closed", merged=True, updated_at="2026-06-12T00:00:00Z")
    reconciler = _reconciler(db, pool, [], closed=[closed_pr])

    assert await reconciler.reconcile_once() == 1
    assert pool.wakes == 1
    delivery_id = reconciled_cleanup_delivery_id("octo/widget", 9, "2026-06-12T00:00:00Z", "abc123")
    row = db.get_event(delivery_id)
    assert row is not None
    assert row.state == "queued"
    assert row.task == "cleanup_workspace"
    assert row.issue_key == "octo/widget#9"
    assert row.payload["action"] == "closed"
    assert row.payload["pull_request"]["number"] == 9
    assert row.payload["pull_request"]["merged"] is True


@pytest.mark.asyncio
async def test_reconciler_skips_closed_cleanup_when_active_event(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.record_event(
        delivery_id="active",
        event_type="pull_request",
        repo="octo/widget",
        issue_key="octo/widget#9",
        payload={"pull_request": {"number": 9}},
        state="running",
        task="review_pr",
        route_reason="pull_request.labeled",
    )
    pool = _FakePool()
    closed_pr = _pr(state="closed", merged=False, updated_at="2026-06-12T00:00:00Z")
    reconciler = _reconciler(db, pool, [], closed=[closed_pr])

    assert await reconciler.reconcile_once() == 0
    assert pool.wakes == 0
    delivery_id = reconciled_cleanup_delivery_id("octo/widget", 9, "2026-06-12T00:00:00Z", "abc123")
    assert db.get_event(delivery_id) is None


@pytest.mark.asyncio
async def test_reconciler_warns_when_webhooks_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db = _db(tmp_path)
    reconciler = _reconciler(db, _FakePool(), [])
    reconciler._webhook_staleness_warn_seconds = 100.0
    # Queued work + stale ingress => loud warning.
    monkeypatch.setattr(db, "seconds_since_last_webhook", lambda: 500.0)
    with caplog.at_level(logging.WARNING):
        reconciler._warn_if_webhooks_stale(2)
    assert any("webhook ingress may be down" in r.getMessage() for r in caplog.records)
    # Fresh ingress, or no queued work, must NOT warn.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        monkeypatch.setattr(db, "seconds_since_last_webhook", lambda: 5.0)
        reconciler._warn_if_webhooks_stale(2)
        monkeypatch.setattr(db, "seconds_since_last_webhook", lambda: 500.0)
        reconciler._warn_if_webhooks_stale(0)
    assert not any("webhook ingress may be down" in r.getMessage() for r in caplog.records)
