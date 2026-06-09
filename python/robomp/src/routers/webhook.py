"""GitHub webhook route."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from robomp import github_events
from robomp.config import OrchestratorSettings
from robomp.db import Database, iso_seconds_ago
from robomp.pr_review_lifecycle import observe_webhook
from robomp.queue import WorkerPool

if TYPE_CHECKING:
    from robomp.server import AppServices

log = logging.getLogger(__name__)
router = APIRouter()


def _services(request: Request) -> AppServices:
    return cast("AppServices", request.app.state.services)


async def _read_body_capped(request: Request, max_bytes: int) -> bytes:
    """Read webhook body with a hard cap before signature verification."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            declared = int(cl)
        except ValueError as exc:
            raise HTTPException(400, "invalid content-length") from exc
        if declared > max_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "request body too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "request body too large")
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body  # type: ignore[attr-defined]
    return body


@router.post("/webhook/github")
async def webhook(
    request: Request,
    x_github_event: str = Header(..., alias="X-GitHub-Event"),
    x_github_delivery: str = Header(..., alias="X-GitHub-Delivery"),
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
) -> JSONResponse:
    services = _services(request)
    cfg: OrchestratorSettings = services.settings
    body = await _read_body_capped(request, cfg.webhook_max_body_bytes)
    if not github_events.verify_signature(
        cfg.github_webhook_secret.get_secret_value(),
        body,
        x_hub_signature_256,
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid json: {exc}") from exc

    db: Database = services.db
    observe_webhook(
        db,
        event_type=x_github_event,
        delivery_id=x_github_delivery,
        payload=payload,
        bot_login=cfg.bot_login,
        allowlist=cfg.repo_allowlist,
    )
    await services.issue_cache.apply_webhook(
        event_type=x_github_event,
        payload=payload,
        allowlist=cfg.repo_allowlist,
    )

    def _resolve(repo_full: str, pr_number: int) -> str | None:
        row = db.find_issue_by_pr(repo_full, pr_number)
        return row.key if row else None

    decision = github_events.route(
        x_github_event,
        payload,
        allowlist=cfg.repo_allowlist,
        bot_login=cfg.bot_login,
        maintainers=cfg.maintainer_logins,
        reviewer_bots=cfg.reviewer_bots,
        pr_review_enabled=cfg.pr_review_enabled,
        pr_review_label_allowlist=cfg.pr_review_label_allowlist,
        resolve_issue_from_pr=_resolve,
    )

    if decision.issue_key:
        cancel_reason: str | None = None
        if (
            x_github_event == "issue_comment"
            and str(payload.get("action") or "") == "created"
            and decision.task == "handle_comment"
        ):
            cancel_reason = "user_replied"
        elif x_github_event == "issues" and str(payload.get("action") or "") == "closed":
            cancel_reason = "externally_closed"
        if cancel_reason is not None:
            cancelled = db.cancel_pending_closure(decision.issue_key, reason=cancel_reason)
            if cancelled:
                log.info(
                    "autoclose cancelled",
                    extra={
                        "issue_key": decision.issue_key,
                        "reason": cancel_reason,
                        "event": x_github_event,
                    },
                )

    if decision.directive:
        payload = dict(payload)
        payload["_robomp_directive"] = {
            "body": decision.directive_body,
            "author": decision.directive_author,
            "pragmas": [list(item) for item in decision.directive_pragmas],
            "authorizes_impl": decision.directive_authorizes_impl,
        }

    if not decision.should_queue:
        log.info("skip", extra={"event": x_github_event, "reason": decision.reason})
        db.record_event(
            delivery_id=x_github_delivery,
            event_type=x_github_event,
            repo=decision.repo,
            issue_key=decision.issue_key,
            payload=payload,
            state="skipped",
            last_error=decision.reason,
            task=decision.task,
            route_reason=decision.reason,
            route_version=1,
        )
        return JSONResponse({"delivery": x_github_delivery, "state": "skipped"}, status_code=202)

    submitter = decision.submitter
    if submitter:
        cap = github_events.rate_limit_cap(
            submitter,
            decision.association,
            unlimited=cfg.rate_limit_unlimited | cfg.maintainer_logins,
            default=cfg.rate_limit_default,
            contributor=cfg.rate_limit_contributor,
        )
        since = iso_seconds_ago(cfg.rate_limit_window_seconds)
        admission = db.admit_submission(
            delivery_id=x_github_delivery,
            login=submitter,
            repo=decision.repo,
            since=since,
            cap=cap,
        )
        if not admission.accepted:
            window = int(cfg.rate_limit_window_seconds)
            reason = f"rate limit: @{submitter} has used {admission.used}/{cap} submissions in the last {window}s"
            log.info(
                "rate_limited",
                extra={
                    "event": x_github_event,
                    "delivery": x_github_delivery,
                    "login": submitter,
                    "association": decision.association,
                    "used": admission.used,
                    "cap": cap,
                },
            )
            db.record_event(
                delivery_id=x_github_delivery,
                event_type=x_github_event,
                repo=decision.repo,
                issue_key=decision.issue_key,
                payload=payload,
                state="skipped",
                last_error=reason,
                task=decision.task,
                route_reason=decision.reason,
                route_version=1,
            )
            return JSONResponse(
                {"delivery": x_github_delivery, "state": "skipped", "reason": "rate_limited"},
                status_code=202,
            )

    inserted = db.record_event(
        delivery_id=x_github_delivery,
        event_type=x_github_event,
        repo=decision.repo,
        issue_key=decision.issue_key,
        payload=payload,
        state="queued",
        task=decision.task,
        route_reason=decision.reason,
        route_version=1,
    )
    if inserted:
        pool: WorkerPool = services.pool
        pool.wake()
        log.info("queued", extra={"event": x_github_event, "delivery": x_github_delivery, "key": decision.issue_key})
    else:
        log.info("duplicate", extra={"event": x_github_event, "delivery": x_github_delivery})
    return JSONResponse({"delivery": x_github_delivery, "state": "queued"}, status_code=202)
