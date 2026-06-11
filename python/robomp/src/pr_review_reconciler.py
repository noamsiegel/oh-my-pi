from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from robomp.db import Database, issue_key
from robomp.github_backend import GitHubBackend
from robomp.github_types import GitHubError, PullRequestInfo
from robomp.pr_review_policy import has_review_label, normalize_label_names

if TYPE_CHECKING:
    from robomp.queue import WorkerPool

log = logging.getLogger(__name__)


def reconciled_delivery_id(repo: str, pr_number: int, head_sha: str) -> str:
    return f"reconcile-pr-review-{repo.replace('/', '__')}-{pr_number}-{head_sha}"


def _payload_for_pr(pr: PullRequestInfo) -> dict[str, object]:
    return {
        "action": "synchronize",
        "repository": {"full_name": pr.repo},
        "pull_request": {
            "number": pr.number,
            "draft": pr.draft,
            "state": pr.state,
            "user": {"login": pr.author, "type": pr.author_type or "User"},
            "head": {"sha": pr.head_sha, "ref": pr.head_ref},
            "base": {"ref": pr.base_ref},
            "labels": [{"name": label} for label in pr.labels],
        },
    }


class PrReviewLabelReconciler:
    """Backstop missed PR label webhooks by polling recent open PRs."""

    def __init__(
        self,
        *,
        db: Database,
        github: GitHubBackend,
        pool: WorkerPool,
        repos: frozenset[str],
        label_allowlist: frozenset[str],
        bot_login: str,
        interval_seconds: float,
        limit_per_repo: int,
    ) -> None:
        self._db = db
        self._github = github
        self._pool = pool
        self._repos = repos
        self._label_allowlist = label_allowlist
        self._bot_login = bot_login.lower()
        self._interval_seconds = max(10.0, float(interval_seconds))
        self._limit_per_repo = max(1, int(limit_per_repo))
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="pr-review-label-reconciler")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None

    async def _loop(self) -> None:
        log.info(
            "pr_review_reconciler online",
            extra={"repos": sorted(self._repos), "interval": self._interval_seconds, "limit": self._limit_per_repo},
        )
        while not self._stop.is_set():
            await self.reconcile_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                pass

    @staticmethod
    def _parse_github_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None

    @staticmethod
    def _time_key(value: str) -> str:
        return value or "0000-00-00T00:00:00Z"

    async def reconcile_once(self) -> int:
        if not self._repos or not self._label_allowlist:
            return 0
        queued = 0
        for repo in sorted(self._repos):
            cursor = self._db.get_pr_review_reconciler_cursor(repo)
            try:
                prs = await self._github.list_open_pull_requests(repo, limit=self._limit_per_repo)
            except GitHubError as exc:
                log.warning("pr_review_reconciler list failed", extra={"repo": repo, "err": str(exc)})
                continue
            max_seen = cursor
            for pr in prs:
                if max_seen is None or self._time_key(pr.updated_at) > self._time_key(max_seen):
                    max_seen = pr.updated_at
                should_enqueue, decision, reason = self._decision(pr, cursor=cursor)
                delivery_id = reconciled_delivery_id(pr.repo, pr.number, pr.head_sha) if pr.head_sha else None
                labels = tuple(sorted(normalize_label_names(pr.labels)))
                self._db.record_pr_review_reconciler_decision(
                    repo=pr.repo,
                    pr_number=pr.number,
                    head_sha=pr.head_sha or "",
                    pr_updated_at=pr.updated_at or None,
                    labels=labels,
                    decision=decision,
                    reason=reason,
                    event_delivery_id=delivery_id,
                )
                if should_enqueue and delivery_id is not None and self._enqueue(pr, delivery_id=delivery_id):
                    queued += 1
            self._db.set_pr_review_reconciler_cursor(repo, max_seen)
        if queued:
            self._pool.wake()
        return queued

    def _decision(self, pr: PullRequestInfo, *, cursor: str | None) -> tuple[bool, str, str]:
        if pr.state != "open":
            return False, "skipped", "not_open"
        if pr.draft:
            return False, "skipped", "draft"
        if pr.author.lower() == self._bot_login or pr.author_type.lower() == "bot":
            return False, "skipped", "bot_authored"
        if not pr.head_sha:
            return False, "skipped", "missing_head_sha"
        if cursor is not None and pr.updated_at and self._time_key(pr.updated_at) < self._time_key(cursor):
            return False, "skipped", "before_reconciler_cursor"
        labels = normalize_label_names(pr.labels)
        if not has_review_label(labels, self._label_allowlist):
            return False, "skipped", "missing_review_trigger_label"
        key = issue_key(pr.repo, pr.number)
        latest = self._db.latest_event_for_issue(key, include_skipped=False)
        if latest is not None and latest.state in {"queued", "running"}:
            return False, "skipped", "active_event_exists"
        if self._db.has_completed_pr_review(pr.repo, pr.number, pr.head_sha):
            return False, "skipped", "completed_review_for_head"
        existing = self._db.get_event(reconciled_delivery_id(pr.repo, pr.number, pr.head_sha))
        if existing is not None:
            return False, "skipped", f"reconciled_event_already_{existing.state}"
        return True, "queued", "missing_webhook_or_unreviewed_head"

    def _enqueue(self, pr: PullRequestInfo, *, delivery_id: str) -> bool:
        key = issue_key(pr.repo, pr.number)
        inserted = self._db.record_event(
            delivery_id=delivery_id,
            event_type="pull_request",
            repo=pr.repo,
            issue_key=key,
            payload=_payload_for_pr(pr),
            state="queued",
            task="review_pr",
            route_reason="pr_review label reconciliation",
            route_version=1,
        )
        if inserted:
            log.info("pr_review_reconciler queued", extra={"repo": pr.repo, "pr": pr.number, "head_sha": pr.head_sha})
        return inserted


__all__ = ["PrReviewLabelReconciler", "reconciled_delivery_id"]
