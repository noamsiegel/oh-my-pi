"""Dashboard and read-only API routes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from robomp.config import OrchestratorSettings
from robomp.dashboard import render_index, tail_jsonl
from robomp.db import Database
from robomp.db import issue_key as make_issue_key
from robomp.github_backend import GitHubBackend
from robomp.github_payloads import issue_summary_from_payload, repo_full_name
from robomp.github_types import IssueSummary
from robomp.queue import WorkerPool

if TYPE_CHECKING:
    from robomp.server import AppServices

log = logging.getLogger(__name__)
router = APIRouter()


@dataclass(slots=True)
class IssueBrowseCacheEntry:
    repos: tuple[str, ...]
    issues: list[IssueSummary]
    errors: list[dict[str, str]]
    fetched_at: float


class IssueBrowseCache:
    """In-process cache for the dashboard's GitHub issue browser."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int, tuple[str, ...]], IssueBrowseCacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get_or_fetch(
        self,
        *,
        state: str,
        limit: int,
        repos: tuple[str, ...],
        force: bool,
        fetch: Callable[[], Awaitable[tuple[list[IssueSummary], list[dict[str, str]]]]],
    ) -> tuple[IssueBrowseCacheEntry, bool]:
        key = (state, limit, repos)
        async with self._lock:
            if not force and (entry := self._entries.get(key)) is not None:
                return entry, True

        issues, errors = await fetch()
        issues.sort(key=lambda s: s.updated_at, reverse=True)
        entry = IssueBrowseCacheEntry(
            repos=repos,
            issues=issues[:limit],
            errors=errors,
            fetched_at=time.time(),
        )
        async with self._lock:
            if not force and (current := self._entries.get(key)) is not None:
                return current, True
            self._entries[key] = entry
            return entry, False

    async def apply_webhook(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        allowlist: frozenset[str],
    ) -> None:
        mutation = _issue_cache_mutation(event_type, payload, allowlist)
        if mutation is None:
            return
        repo, number, summary = mutation
        async with self._lock:
            for (state, limit, repos), entry in self._entries.items():
                if repo not in repos:
                    continue
                entry.issues = [item for item in entry.issues if not (item.repo == repo and item.number == number)]
                if summary is not None and _cache_state_includes(state, summary.state):
                    entry.issues.append(summary)
                    entry.issues.sort(key=lambda s: s.updated_at, reverse=True)
                    del entry.issues[limit:]


def _cache_state_includes(cache_state: str, issue_state: str) -> bool:
    return cache_state == "all" or issue_state == cache_state




def _issue_cache_mutation(
    event_type: str,
    payload: Mapping[str, Any],
    allowlist: frozenset[str],
) -> tuple[str, int, IssueSummary | None] | None:
    if event_type not in {"issues", "issue_comment"}:
        return None
    repo = repo_full_name(payload)
    if repo is None or repo.lower() not in allowlist:
        return None
    issue = payload.get("issue")
    if not isinstance(issue, Mapping):
        return None
    number = issue.get("number")
    if not isinstance(number, int):
        return None
    if "pull_request" in issue:
        return repo, number, None
    if str(payload.get("action") or "") == "deleted":
        return repo, number, None
    summary = issue_summary_from_payload(repo, issue)
    if summary is None:
        return None
    return repo, number, summary


def _issue_browse_payload(
    *,
    entry: IssueBrowseCacheEntry,
    cache_hit: bool,
    processed_keys: frozenset[str],
) -> dict[str, Any]:
    return {
        "issues": [
            {
                "repo": s.repo,
                "number": s.number,
                "title": s.title,
                "state": s.state,
                "author": s.author,
                "labels": list(s.labels),
                "comments": s.comments,
                "updated_at": s.updated_at,
                "created_at": s.created_at,
                "html_url": s.html_url,
                "processed": make_issue_key(s.repo, s.number) in processed_keys,
            }
            for s in entry.issues
        ],
        "errors": [dict(error) for error in entry.errors],
        "repos": list(entry.repos),
        "cache": {"hit": cache_hit, "fetched_at": entry.fetched_at},
    }


def _services(request: Request) -> AppServices:
    return cast("AppServices", request.app.state.services)


def _require_trigger_token(cfg: OrchestratorSettings, token: str | None) -> None:
    if cfg.replay_token is None:
        raise HTTPException(404, "trigger disabled (set ROBOMP_REPLAY_TOKEN to enable)")
    if token != cfg.replay_token.get_secret_value():
        raise HTTPException(401, "invalid replay token")


@router.get("/api/pr-review/self-improvements")
async def api_pr_review_self_improvements(request: Request) -> dict[str, Any]:
    db: Database = _services(request).db
    recs: list[Mapping[str, Any]] = []
    for status_name in ("candidate", "implemented", "rejected"):
        recs.extend(db.list_pr_review_self_improvement_recommendations(status=status_name, limit=100))
    return {
        "runs": db.list_pr_review_self_improvement_runs(limit=20),
        "recommendations": recs[:100],
    }


@router.get("/api/github/issues")
async def api_github_issues(
    request: Request,
    state: str = "open",
    limit: int = 30,
    refresh: bool = False,
    x_robomp_token: str | None = Header(None, alias="X-Robomp-Replay-Token"),
) -> dict[str, Any]:
    services = _services(request)
    cfg: OrchestratorSettings = services.settings
    _require_trigger_token(cfg, x_robomp_token)

    if state not in ("open", "closed", "all"):
        raise HTTPException(400, "state must be open|closed|all")
    capped = max(1, min(int(limit), 100))
    github: GitHubBackend = services.github
    issue_cache = services.issue_cache
    repos = tuple(sorted(cfg.repo_allowlist))
    if not repos:
        return {"issues": [], "errors": [], "repos": [], "cache": {"hit": False, "fetched_at": time.time()}}

    async def _fetch() -> tuple[list[IssueSummary], list[dict[str, str]]]:
        async def _one(repo: str) -> tuple[str, list[IssueSummary], str | None]:
            try:
                items = await github.list_issues(repo, state=state, limit=capped)
                return repo, items, None
            except Exception as exc:
                log.warning("list_issues failed", extra={"repo": repo, "err": str(exc)})
                return repo, [], str(exc)

        results = await asyncio.gather(*(_one(r) for r in repos))
        merged: list[IssueSummary] = []
        errors: list[dict[str, str]] = []
        for repo, items, err in results:
            if err is not None:
                errors.append({"repo": repo, "error": err})
            merged.extend(items)
        return merged, errors

    entry, cache_hit = await issue_cache.get_or_fetch(
        state=state,
        limit=capped,
        repos=repos,
        force=refresh,
        fetch=_fetch,
    )
    db: Database = services.db
    processed = frozenset(db.processed_issue_keys(make_issue_key(s.repo, s.number) for s in entry.issues))
    return _issue_browse_payload(entry=entry, cache_hit=cache_hit, processed_keys=processed)


@router.get("/events")
async def events(request: Request, limit: int = 50) -> dict[str, Any]:
    rows = _services(request).db.list_events(limit=limit)
    return {
        "events": [
            {
                "delivery_id": r.delivery_id,
                "event_type": r.event_type,
                "repo": r.repo,
                "issue_key": r.issue_key,
                "state": r.state,
                "attempts": r.attempts,
                "received_at": r.received_at,
                "last_error": r.last_error,
                "task": r.task,
                "route_reason": r.route_reason,
                "route_version": r.route_version,
                "outcome": r.outcome,
            }
            for r in rows
        ]
    }


@router.get("/issues")
async def issues(request: Request, limit: int = 100) -> dict[str, Any]:
    rows = _services(request).db.list_issues(limit=limit)
    return {
        "issues": [
            {
                "key": r.key,
                "repo": r.repo,
                "number": r.number,
                "branch": r.branch,
                "pr_number": r.pr_number,
                "state": r.state,
                "classification": r.classification,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    cfg: OrchestratorSettings = _services(request).settings
    token = cfg.replay_token.get_secret_value() if cfg.replay_token else None
    return HTMLResponse(render_index(token))


@router.get("/api/status")
async def api_status(request: Request) -> dict[str, Any]:
    services = _services(request)
    cfg: OrchestratorSettings = services.settings
    db: Database = services.db
    pool: WorkerPool = services.pool
    started = float(getattr(request.app.state, "started_at", time.time()) or time.time())
    issues_rows = db.list_issues(limit=200)
    latest_events = db.latest_events_for_issues(r.key for r in issues_rows)

    def _latest_event_payload(key: str) -> dict[str, Any] | None:
        latest = latest_events.get(key)
        if latest is None:
            return None
        return {
            "delivery_id": latest.delivery_id,
            "event_type": latest.event_type,
            "state": latest.state,
            "attempts": latest.attempts,
            "received_at": latest.received_at,
            "last_error": latest.last_error,
            "task": latest.task,
            "route_reason": latest.route_reason,
            "route_version": latest.route_version,
            "outcome": latest.outcome,
        }

    events_rows = db.list_events(limit=25)
    return {
        "runtime": {
            "bot_login": cfg.bot_login,
            "repo_allowlist": sorted(cfg.repo_allowlist),
            "max_concurrency": cfg.max_concurrency,
            "model": cfg.model,
            "thinking_level": cfg.thinking_level,
            "uptime_seconds": max(0.0, time.time() - started),
        },
        "event_counts": db.event_state_counts(),
        "issue_event_counts": db.latest_issue_event_state_counts(),
        "running_events": db.list_running_events(),
        "inflight": await pool.inflight_snapshot(),
        "issues": [
            {
                "key": r.key,
                "repo": r.repo,
                "number": r.number,
                "branch": r.branch,
                "pr_number": r.pr_number,
                "state": r.state,
                "classification": r.classification,
                "updated_at": r.updated_at,
                "latest_event": _latest_event_payload(r.key),
            }
            for r in issues_rows
        ],
        "recent_events": [
            {
                "delivery_id": r.delivery_id,
                "event_type": r.event_type,
                "repo": r.repo,
                "issue_key": r.issue_key,
                "state": r.state,
                "attempts": r.attempts,
                "received_at": r.received_at,
                "last_error": r.last_error,
                "task": r.task,
                "route_reason": r.route_reason,
                "route_version": r.route_version,
                "outcome": r.outcome,
            }
            for r in events_rows
        ],
    }


@router.get("/api/logs")
async def api_logs(request: Request, limit: int = 400) -> dict[str, Any]:
    cfg: OrchestratorSettings = _services(request).settings
    capped = max(1, min(int(limit), 2000))
    entries = tail_jsonl(cfg.log_dir / "robomp.log.jsonl", limit=capped)
    return {"entries": entries, "count": len(entries), "limit": capped}
