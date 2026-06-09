"""Operator-triggered routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from robomp.config import OrchestratorSettings
from robomp.db import INACTIVE_EVENT_STATES, Database
from robomp.db import issue_key as make_issue_key
from robomp.github_backend import GitHubBackend
from robomp.github_types import GitHubError
from robomp.manual_triage import (
    InvalidIssueRef,
    ManualTriageConflict,
    ManualTriageError,
    enqueue_manual_triage,
    parse_issue_ref,
)
from robomp.queue import WorkerPool

if TYPE_CHECKING:
    from robomp.server import AppServices

log = logging.getLogger(__name__)
router = APIRouter()


def _services(request: Request) -> AppServices:
    return cast("AppServices", request.app.state.services)


def _require_trigger_token(cfg: OrchestratorSettings, token: str | None) -> None:
    if cfg.replay_token is None:
        raise HTTPException(404, "trigger disabled (set ROBOMP_REPLAY_TOKEN to enable)")
    if token != cfg.replay_token.get_secret_value():
        raise HTTPException(401, "invalid replay token")


@router.post("/replay")
async def replay(
    request: Request,
    x_robomp_token: str | None = Header(None, alias="X-Robomp-Replay-Token"),
    delivery_id: str = "",
) -> JSONResponse:
    services = _services(request)
    cfg: OrchestratorSettings = services.settings
    if cfg.replay_token is None:
        raise HTTPException(404, "replay disabled")
    if x_robomp_token != cfg.replay_token.get_secret_value():
        raise HTTPException(401, "invalid replay token")
    db: Database = services.db
    row = db.get_event(delivery_id)
    if row is None:
        raise HTTPException(404, "unknown delivery")
    if not db.requeue_event(delivery_id, from_states=INACTIVE_EVENT_STATES):
        raise HTTPException(409, f"delivery {delivery_id} is {row.state}; only inactive events can be replayed")
    services.pool.wake()
    return JSONResponse({"delivery": delivery_id, "state": "queued"})


@router.post("/api/trigger")
async def api_trigger(
    request: Request,
    payload: dict[str, Any] = Body(...),
    x_robomp_token: str | None = Header(None, alias="X-Robomp-Replay-Token"),
) -> JSONResponse:
    services = _services(request)
    cfg: OrchestratorSettings = services.settings
    _require_trigger_token(cfg, x_robomp_token)

    db: Database = services.db
    github: GitHubBackend = services.github
    pool: WorkerPool = services.pool

    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in ("triage", "retry"):
        raise HTTPException(400, "mode must be 'triage' or 'retry'")

    issue_ref = payload.get("issue")
    delivery_id = payload.get("delivery_id")

    if mode == "triage":
        if not isinstance(issue_ref, str) or not issue_ref:
            raise HTTPException(400, "triage requires 'issue' = 'owner/repo#NN'")
        try:
            repo_full, number = parse_issue_ref(issue_ref)
        except InvalidIssueRef as exc:
            raise HTTPException(400, str(exc)) from exc
        if not cfg.allows(repo_full):
            raise HTTPException(403, f"{repo_full} not in ROBOMP_REPO_ALLOWLIST")
        try:
            delivery = await enqueue_manual_triage(
                db=db,
                github=github,
                repo_full=repo_full,
                number=number,
            )
        except ManualTriageConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ManualTriageError as exc:
            raise HTTPException(400, str(exc)) from exc
        except GitHubError as exc:
            raise HTTPException(502, f"github error: {exc.status} {exc.message}") from exc
        pool.wake()
        log.info("manual triage", extra={"delivery": delivery, "issue": f"{repo_full}#{number}"})
        return JSONResponse(
            {"delivery": delivery, "state": "queued", "mode": "triage"},
            status_code=202,
        )

    if isinstance(delivery_id, str) and delivery_id:
        target = delivery_id
    elif isinstance(issue_ref, str) and issue_ref:
        try:
            repo_full, number = parse_issue_ref(issue_ref)
        except InvalidIssueRef as exc:
            raise HTTPException(400, str(exc)) from exc
        if not cfg.allows(repo_full):
            raise HTTPException(403, f"{repo_full} not in ROBOMP_REPO_ALLOWLIST")
        row = db.latest_event_for_issue(make_issue_key(repo_full, number))
        if row is None:
            raise HTTPException(404, f"no retryable stored event for {repo_full}#{number}")
        target = row.delivery_id
    else:
        raise HTTPException(400, "retry requires 'delivery_id' or 'issue'")

    event = db.get_event(target)
    if event is None:
        raise HTTPException(404, f"unknown delivery {target}")
    if not db.requeue_event(target, from_states=INACTIVE_EVENT_STATES):
        raise HTTPException(409, f"delivery {target} is {event.state}; only inactive events can be retried")
    pool.wake()
    log.info("manual retry", extra={"delivery": target})
    return JSONResponse(
        {"delivery": target, "state": "queued", "mode": "retry"},
        status_code=202,
    )


@router.post("/api/cancel")
async def api_cancel(
    request: Request,
    payload: dict[str, Any] = Body(...),
    x_robomp_token: str | None = Header(None, alias="X-Robomp-Replay-Token"),
) -> JSONResponse:
    services = _services(request)
    cfg: OrchestratorSettings = services.settings
    _require_trigger_token(cfg, x_robomp_token)

    delivery_id = payload.get("delivery_id")
    if not isinstance(delivery_id, str) or not delivery_id:
        raise HTTPException(400, "cancel requires 'delivery_id'")

    db: Database = services.db
    event = db.get_event(delivery_id)
    if event is None:
        raise HTTPException(404, f"unknown delivery {delivery_id}")

    fired = await services.pool.cancel_event(delivery_id)
    log.info(
        "manual cancel",
        extra={"delivery": delivery_id, "fired": fired, "state": event.state},
    )
    return JSONResponse(
        {"delivery": delivery_id, "fired": fired, "previous_state": event.state},
        status_code=202,
    )
