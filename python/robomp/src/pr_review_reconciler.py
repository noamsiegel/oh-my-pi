from __future__ import annotations

import asyncio
import hashlib
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


def reconciled_cleanup_delivery_id(repo: str, pr_number: int, updated_at: str, head_sha: str) -> str:
    source = updated_at or head_sha or "unknown"
    suffix = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
    return f"reconcile-pr-cleanup-{repo.replace('/', '__')}-{pr_number}-{suffix}"


def _closed_payload_for_pr(pr: PullRequestInfo) -> dict[str, object]:
    return {
        "action": "closed",
        "repository": {"full_name": pr.repo},
        "pull_request": {
            "number": pr.number,
            "state": pr.state,
            "merged": pr.merged,
            "user": {"login": pr.author, "type": pr.author_type or "User"},
            "head": {"sha": pr.head_sha},
            "base": {"ref": pr.base_ref},
            "labels": [{"name": label} for label in pr.labels],
        },
    }


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
        ci_gate_enabled: bool = False,
        webhook_staleness_warn_seconds: float = 1800.0,
    ) -> None:
        self._db = db
        self._github = github
        self._pool = pool
        self._repos = repos
        self._label_allowlist = label_allowlist
        self._bot_login = bot_login.lower()
        self._interval_seconds = max(10.0, float(interval_seconds))
        self._limit_per_repo = max(1, int(limit_per_repo))
        self._ci_gate_enabled = ci_gate_enabled
        self._webhook_staleness_warn_seconds = webhook_staleness_warn_seconds
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
            queued = await self.reconcile_once()
            self._warn_if_webhooks_stale(queued)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                pass

    def _warn_if_webhooks_stale(self, queued: int) -> None:
        """Loud signal for a dead webhook ingress: if this cycle had to queue
        reviews (work the webhooks should have delivered) yet no native webhook
        has arrived within the threshold, deliveries are almost certainly down
        and the reconciler is silently carrying the load. This is what would
        have surfaced the 8-day outage on day one. ``<=0`` threshold disables."""
        threshold = self._webhook_staleness_warn_seconds
        if threshold <= 0 or queued <= 0:
            return
        age = self._db.seconds_since_last_webhook()
        if age is not None and age > threshold:
            log.warning(
                "webhook ingress may be down: reconciler queued work but no native GitHub webhook received recently",
                extra={"queued": queued, "seconds_since_last_webhook": round(age, 1), "threshold": threshold},
            )

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
                prs = await self._github.list_pull_requests(repo, state="open", limit=self._limit_per_repo)
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
            queued += await self._reconcile_closed_cleanup(repo)
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
            task="probe_pr_review_ci" if self._ci_gate_enabled else "review_pr",
            route_reason="pr_review label reconciliation",
            route_version=1,
        )
        if inserted:
            log.info("pr_review_reconciler queued", extra={"repo": pr.repo, "pr": pr.number, "head_sha": pr.head_sha})
        return inserted

    async def _reconcile_closed_cleanup(self, repo: str) -> int:
        """Enqueue synthetic ``pull_request.closed`` cleanup events for closed PRs.

        Reconciler-driven deployments may miss close webhooks, leaving old PR-head
        workspaces forever; scanning recently-closed PRs reconciles them into the
        same cleanup path a close webhook would have taken.
        """
        try:
            closed_prs = await self._github.list_pull_requests(repo, state="closed", limit=self._limit_per_repo)
        except GitHubError as exc:
            log.warning("pr_review_reconciler closed list failed", extra={"repo": repo, "err": str(exc)})
            return 0
        queued = 0
        for pr in closed_prs:
            should_enqueue, reason = self._closed_cleanup_decision(pr)
            if not should_enqueue:
                continue
            delivery_id = reconciled_cleanup_delivery_id(pr.repo, pr.number, pr.updated_at, pr.head_sha)
            if self._enqueue_cleanup(pr, delivery_id=delivery_id, reason=reason):
                queued += 1
        return queued

    def _closed_cleanup_decision(self, pr: PullRequestInfo) -> tuple[bool, str]:
        if pr.state != "closed":
            return False, "not_closed"
        if pr.author.lower() == self._bot_login or pr.author_type.lower() == "bot":
            return False, "bot_authored"
        key = issue_key(pr.repo, pr.number)
        latest = self._db.latest_event_for_issue(key, include_skipped=False)
        if latest is not None and latest.state in {"queued", "running"}:
            return False, "active_event_exists"
        delivery_id = reconciled_cleanup_delivery_id(pr.repo, pr.number, pr.updated_at, pr.head_sha)
        if self._db.get_event(delivery_id) is not None:
            return False, "cleanup_event_already_exists"
        return True, "closed_pr_reconciled_cleanup"

    def _enqueue_cleanup(self, pr: PullRequestInfo, *, delivery_id: str, reason: str) -> bool:
        key = issue_key(pr.repo, pr.number)
        inserted = self._db.record_event(
            delivery_id=delivery_id,
            event_type="pull_request",
            repo=pr.repo,
            issue_key=key,
            payload=_closed_payload_for_pr(pr),
            state="queued",
            task="cleanup_workspace",
            route_reason=reason,
            route_version=1,
        )
        if inserted:
            log.info(
                "pr_review_reconciler cleanup queued",
                extra={"repo": pr.repo, "pr": pr.number, "merged": pr.merged},
            )
        return inserted


__all__ = ["PrReviewLabelReconciler", "reconciled_cleanup_delivery_id", "reconciled_delivery_id"]
