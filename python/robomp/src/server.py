"""FastAPI receiver for GitHub webhooks."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from robomp.autoclose import AutocloseScheduler
from robomp.config import OrchestratorSettings, get_settings
from robomp.dashboard import static_dir
from robomp.db import Database, get_database
from robomp.github_backend import GitHubBackend
from robomp.natives_cache import NativesCache
from robomp.pr_review_self_improver import PrReviewSelfImprovementScheduler
from robomp.proxy_client import GitHubProxyClient, ProxyGitTransport
from robomp.queue import WorkerPool
from robomp.routers import dashboard, operator, webhook
from robomp.routers.dashboard import IssueBrowseCache
from robomp.sandbox import GitTransport, SandboxManager

log = logging.getLogger(__name__)
_RECENT_FAILURE_WINDOW_SECONDS = 3600.0


def _log_dir_ready(path: Any) -> bool:
    return os.path.isdir(path) and os.access(path, os.W_OK)


async def _check_gh_proxy_health(cfg: OrchestratorSettings) -> bool:
    try:
        async with httpx.AsyncClient(
            base_url=cfg.gh_proxy_url.rstrip("/"),
            timeout=httpx.Timeout(2.0, connect=2.0),
        ) as client:
            resp = await client.get("/healthz")
    except Exception:
        log.warning("gh-proxy health check failed", exc_info=True)
        return False
    if resp.status_code != 200:
        return False
    try:
        return resp.json().get("status") == "ok"
    except Exception:
        return False


@dataclass(slots=True)
class AppServices:
    settings: OrchestratorSettings
    db: Database
    github: GitHubBackend
    git_transport: GitTransport
    sandbox: SandboxManager
    pool: WorkerPool
    autoclose: Any
    self_improver: PrReviewSelfImprovementScheduler | None
    issue_cache: IssueBrowseCache


def _require_proxy_mode(cfg: OrchestratorSettings) -> tuple[str, bytes]:
    if cfg.github_token is not None:
        raise SystemExit(
            "robomp orchestrator refuses to start with GITHUB_TOKEN set in env. "
            "The PAT must live only in the gh-proxy container."
        )
    return cfg.gh_proxy_url, cfg.gh_proxy_hmac_key.get_secret_value().encode("utf-8")


def _build_orchestrator(cfg: OrchestratorSettings) -> tuple[GitHubBackend, ProxyGitTransport]:
    base_url, key = _require_proxy_mode(cfg)
    github = GitHubProxyClient(base_url=base_url, hmac_key=key, timeout=cfg.request_timeout_seconds)
    git_timeout = max(cfg.request_timeout_seconds, cfg.gh_proxy_git_timeout_seconds + 10.0)
    transport = ProxyGitTransport(base_url=base_url, hmac_key=key, timeout=git_timeout)
    return github, transport


def _build_services(settings: OrchestratorSettings) -> AppServices:
    db = get_database(settings.sqlite_path)
    github, git_transport = _build_orchestrator(settings)
    natives_cache: NativesCache | None = None
    if settings.natives_cache_enabled:
        natives_cache = NativesCache(
            settings.natives_cache_root,
            max_entries_per_repo=settings.natives_cache_max_entries_per_repo,
            max_bytes=settings.natives_cache_max_bytes,
        )
    sandbox = SandboxManager(
        settings.workspace_root,
        transport=git_transport,
        natives_cache=natives_cache,
    )
    pool = WorkerPool(settings=settings, db=db, github=github, sandbox=sandbox, git_transport=git_transport)
    self_improver = PrReviewSelfImprovementScheduler(settings=settings, db=db)
    autoclose = AutocloseScheduler(settings=settings, db=db, github=github)
    return AppServices(
        settings=settings,
        db=db,
        github=github,
        git_transport=git_transport,
        sandbox=sandbox,
        pool=pool,
        autoclose=autoclose,
        self_improver=self_improver,
        issue_cache=IssueBrowseCache(),
    )


def create_app(settings: OrchestratorSettings | None = None) -> FastAPI:
    """Build the FastAPI app. `settings` parameter is for tests."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or get_settings()
        cfg.ensure_paths()
        services = _build_services(cfg)
        app.state.services = services
        app.state.started_at = time.time()
        await services.pool.start()
        if services.self_improver is not None:
            await services.self_improver.start()
        await services.autoclose.start()
        try:
            yield
        finally:
            if services.self_improver is not None:
                await services.self_improver.stop()
            await services.autoclose.stop()
            await services.pool.stop(
                drain_timeout=cfg.shutdown_drain_timeout_seconds,
                kill_timeout=cfg.shutdown_kill_timeout_seconds,
            )
            aclose = getattr(services.github, "aclose", None)
            if aclose is not None:
                await aclose()

    app = FastAPI(title="robomp", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", response_model=None)
    async def readyz(request: Request) -> Any:
        services = getattr(request.app.state, "services", None)
        if services is None:
            return JSONResponse({"status": "not_ready", "checks": {"services": False}}, status_code=503)
        checks: dict[str, bool] = {
            "db": False,
            "log_dir": _log_dir_ready(services.settings.log_dir),
            "worker_pool": services.pool.started,
            "gh_proxy": await _check_gh_proxy_health(services.settings),
        }
        try:
            services.db.event_counts_by_state()
            checks["db"] = True
        except Exception:
            log.warning("database readiness check failed", exc_info=True)
        if all(checks.values()):
            return {"status": "ready"}
        return JSONResponse({"status": "not_ready", "checks": checks}, status_code=503)

    @app.get("/metrics")
    async def metrics(request: Request) -> PlainTextResponse:
        services = getattr(request.app.state, "services", None)
        if services is None:
            return PlainTextResponse("robomp_ready 0\n", status_code=503)
        counts = services.db.event_counts_by_state()
        oldest = services.db.oldest_queued_age_seconds()
        recent_failures = services.db.recent_failure_count(_RECENT_FAILURE_WINDOW_SECONDS)
        inflight = await services.pool.inflight_snapshot()
        lines = [
            f"robomp_events_queued {counts['queued']}",
            f"robomp_events_running {counts['running']}",
            f"robomp_events_done {counts['done']}",
            f"robomp_events_failed {counts['failed']}",
            f"robomp_events_skipped {counts['skipped']}",
            f"robomp_oldest_queued_age_seconds {oldest if oldest is not None else 0.0:.3f}",
            f"robomp_recent_failures_total {recent_failures}",
            f"robomp_inflight {len(inflight)}",
        ]
        latest_self_improvement = services.db.latest_pr_review_self_improvement_run()
        if latest_self_improvement is not None:
            status_name = str(latest_self_improvement.get("status") or "unknown")
            lines.append(f'robomp_self_improver_last_status{{status="{status_name}"}} 1')
        return PlainTextResponse("\n".join(lines) + "\n")

    app.include_router(webhook.router)
    app.include_router(dashboard.router)
    app.include_router(operator.router)

    # Mount the built dashboard bundle. The `index.html` itself is served by
    # the `@app.get("/")` handler in `routers.dashboard` so the per-instance
    # replay-token can be substituted; `/static/*` carries hashed JS/CSS.
    app.mount("/static", StaticFiles(directory=static_dir()), name="static")

    return app


__all__ = ["AppServices", "create_app"]
