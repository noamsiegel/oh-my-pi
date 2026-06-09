"""Tests for the WorkerPool dispatcher loop and same-issue serialization."""

from __future__ import annotations

import asyncio

import pytest

from robomp.config import Settings
from robomp.db import Database, issue_key
from robomp.queue import WorkerPool


class _StubSandbox:
    """Sentinel; queue tests don't touch the workspace pool."""

    natives_cache = None


class _StubGitTransport:
    """Sentinel; queue tests don't push."""


class _StubGitHub:
    """Sentinel; queue tests don't talk to GitHub."""


def _make_pool(settings: Settings, db: Database) -> WorkerPool:
    return WorkerPool(
        settings=settings,
        db=db,
        github=_StubGitHub(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        git_transport=_StubGitTransport(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_real_dispatcher_loop_with_triage_issue_no_op(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real dispatcher-loop test: db.record_event, monkeypatch tasks.triage_issue async no-op, WorkerPool.start, pool.wake, bounded poll until done, assert _inflight_tasks empty, await pool.stop."""
    from robomp import tasks

    pool = _make_pool(settings, db)

    # Monkeypatch tasks.triage_issue to async no-op
    async def _triage_no_op(**kwargs):
        pass

    monkeypatch.setattr(tasks, "triage_issue", _triage_no_op)

    # Record event
    db.record_event(
        delivery_id="dispatcher-test",
        event_type="manual",
        repo="octo/widget",
        issue_key=issue_key("octo/widget", 42),
        payload={"action": "opened", "issue": {"number": 42}, "repository": {"full_name": "octo/widget"}},
        task="triage_issue",
    )
    # Start pool
    await pool.start()

    # Wake dispatcher
    pool.wake()

    # Bounded poll until DB state is done
    for _ in range(100):  # Max 10 seconds at 100ms intervals
        await asyncio.sleep(0.1)
        event = db.get_event("dispatcher-test")
        if event and event.state == "done":
            break
    else:
        pytest.fail("Event did not complete within timeout")
    # Assert _inflight_tasks empty
    assert len(pool._inflight_tasks) == 0

    # Stop pool
    await pool.stop()

    # Verify final state
    final_event = db.get_event("dispatcher-test")
    assert final_event.state == "done"


@pytest.mark.asyncio
async def test_dispatcher_marks_unsupported_event_task_skipped(settings: Settings, db: Database) -> None:
    pool = _make_pool(settings, db)
    db.record_event(
        delivery_id="unsupported-test",
        event_type="unknown",
        repo="octo/widget",
        issue_key=issue_key("octo/widget", 43),
        payload={"action": "mystery"},
    )

    await pool.start()
    pool.wake()

    for _ in range(100):
        await asyncio.sleep(0.1)
        event = db.get_event("unsupported-test")
        if event and event.state == "skipped":
            break
    else:
        pytest.fail("Event did not skip within timeout")

    await pool.stop()
    final_event = db.get_event("unsupported-test")
    assert final_event.state == "skipped"
    assert final_event.last_error == "unsupported event/task"
    assert final_event.outcome == "skipped"


@pytest.mark.asyncio
async def test_same_issue_two_event_dispatcher_serialization(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-issue two-event dispatcher test using asyncio.Event to hold first task and prove second remains queued until first finishes."""
    from robomp import tasks

    pool = _make_pool(settings, db)
    first_task_started = asyncio.Event()
    first_task_hold = asyncio.Event()

    async def _triage_with_hold(**kwargs):
        first_task_started.set()
        await first_task_hold.wait()

    monkeypatch.setattr(tasks, "triage_issue", _triage_with_hold)

    # Record two events for same issue
    same_key = issue_key("octo/widget", 42)
    db.record_event(
        delivery_id="first-event",
        event_type="issues",
        repo="octo/widget",
        issue_key=same_key,
        payload={"action": "opened", "issue": {"number": 42}, "repository": {"full_name": "octo/widget"}},
    )
    db.record_event(
        delivery_id="second-event",
        event_type="issues",
        repo="octo/widget",
        issue_key=same_key,
        payload={"action": "labeled", "issue": {"number": 42}, "repository": {"full_name": "octo/widget"}},
        task="triage_issue",
    )

    # Start pool
    await pool.start()

    # Wake dispatcher
    pool.wake()

    # Wait for first task to start
    await first_task_started.wait()

    # Give dispatcher a moment to process both events
    await asyncio.sleep(0.2)

    # Assert first event is running, second is still queued
    first_event = db.get_event("first-event")
    second_event = db.get_event("second-event")
    assert first_event.state == "running"
    assert second_event.state == "queued"  # Still queued due to same-issue serialization
    assert second_event.attempts == 0

    # Assert only one task in _inflight_tasks
    assert len(pool._inflight_tasks) == 1

    # Release first task
    first_task_hold.set()

    # Wait for both events to complete
    for _ in range(100):  # Max 10 seconds
        await asyncio.sleep(0.1)
        first_final = db.get_event("first-event")
        second_final = db.get_event("second-event")
        if first_final and second_final and first_final.state == "done" and second_final.state == "done":
            break
    else:
        pytest.fail("Events did not complete within timeout")

    # Assert _inflight_tasks empty
    assert len(pool._inflight_tasks) == 0

    # Stop pool
    await pool.stop()

    # Verify final states
    first_final = db.get_event("first-event")
    second_final = db.get_event("second-event")
    assert first_final.state == "done"
    assert second_final.state == "done"
