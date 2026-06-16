"""Task entry points dispatched off the durable event queue."""

from __future__ import annotations

import asyncio
import os
import subprocess
import logging
from datetime import UTC, datetime
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from robomp import persona
from robomp.config import Settings
from robomp.db import Database, IssueRow, IssueState, PrReviewGateTransition, issue_key
from robomp.github_backend import GitHubBackend
from robomp.github_payloads import parse_issue_payload, repo_full_name
from robomp.github_types import (
    CommentInfo,
    GitHubError,
    IssueInfo,
    PullRequestCiStatusInfo,
    PullRequestInfo,
    PullRequestReviewInfo,
    RepoInfo,
)
from robomp.pr_review_suggestion_fastpath import accepted_suggestions_fast_path_result
from robomp.pr_review_policy import matching_review_labels, normalize_label_names
from robomp.pr_review_tools import pr_review_comment_retryable
from robomp.sandbox import GitTransport, SandboxManager, Workspace
from robomp.task_outcome import DeferredTask, TaskOutcome, TransientTaskError
from robomp.worker import DirectiveInfo, PrReviewFocus, TaskInputs, ThreadMessage, run_task

log = logging.getLogger(__name__)

def _skipped(reason: str) -> TaskOutcome:
    return TaskOutcome("skipped", reason)


def _github_fetch_failed(exc: GitHubError) -> TransientTaskError:
    return TransientTaskError(f"GitHub fetch failed: {exc}", retry_delay_seconds=exc.retry_after)


def _elapsed_since_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - dt).total_seconds())


def _ci_gate_summary(ci: PullRequestCiStatusInfo) -> str:
    return f"state={ci.state} total={ci.total_count} pending={ci.pending_count} failed={ci.failed_count}"


def _expected_head_sha(payload: Mapping[str, Any]) -> str | None:
    """Head SHA the event was queued for (reconciler payload + native webhooks
    both carry ``pull_request.head.sha``). Lets the worker skip a review whose
    head was superseded by a newer push before it ran, instead of reviewing or
    gate-commenting on a now-stale head."""
    pr = payload.get("pull_request")
    if not isinstance(pr, Mapping):
        return None
    head = pr.get("head")
    if not isinstance(head, Mapping):
        return None
    sha = head.get("sha")
    return sha if isinstance(sha, str) and sha else None


def _event_head_sha(payload: Mapping[str, Any]) -> str | None:
    """Head SHA an event concerns, across PR + CI webhook shapes — used to
    debounce a burst of CI webhooks for the same head before any GitHub fetch."""
    pr = payload.get("pull_request")
    if isinstance(pr, Mapping):
        head = pr.get("head")
        if isinstance(head, Mapping) and isinstance(head.get("sha"), str) and head["sha"]:
            return head["sha"]
    for node_key in ("check_run", "check_suite"):
        node = payload.get(node_key)
        if isinstance(node, Mapping):
            sha = node.get("head_sha")
            if isinstance(sha, str) and sha:
                return sha
            suite = node.get("check_suite")
            if isinstance(suite, Mapping) and isinstance(suite.get("head_sha"), str) and suite["head_sha"]:
                return suite["head_sha"]
    sha = payload.get("sha")  # status event
    return sha if isinstance(sha, str) and sha else None


def _pr_review_ci_gate_outcome(
    settings: Settings,
    ci: PullRequestCiStatusInfo,
    *,
    received_at: str | None,
) -> TaskOutcome | None:
    if not settings.pr_review_ci_gate_enabled:
        return None
    if ci.state == "passed":
        return None
    summary = _ci_gate_summary(ci)
    if ci.state == "failed":
        return _skipped(f"skip: PR CI checks failed ({summary})")
    elapsed = _elapsed_since_iso(received_at)
    if elapsed is not None and elapsed >= settings.pr_review_ci_gate_timeout_seconds:
        return _skipped(f"skip: PR CI checks did not complete before timeout ({summary})")
    return DeferredTask(
        f"waiting for PR CI checks ({summary})",
        retry_delay_seconds=settings.pr_review_ci_gate_retry_seconds,
    ).outcome


def _direct_pr_skip_reason(*, settings: Settings, repo_full: str, pr: PullRequestInfo) -> str | None:
    if not pr.head_ref:
        reason = "skip: PR has no head ref"
        log.info(reason, extra={"repo": repo_full, "pr": pr.number})
        return reason
    if pr.author.lower() != settings.bot_login.lower():
        reason = "skip: unmapped PR not authored by bot"
        log.info(reason, extra={"repo": repo_full, "pr": pr.number, "author": pr.author})
        return reason
    if pr.head_repo.lower() != repo_full.lower():
        reason = "skip: unmapped PR head is not this repo"
        log.info(reason, extra={"repo": repo_full, "pr": pr.number, "head_repo": pr.head_repo})
        return reason
    return None


def _comment_from_payload(payload: Mapping[str, Any]) -> CommentInfo:
    c = payload.get("comment") or {}
    user = c.get("user") or {}
    return CommentInfo(
        id=int(c.get("id") or 0),
        author=str(user.get("login") or ""),
        body=str(c.get("body") or ""),
        created_at=str(c.get("created_at") or ""),
    )


def _directive_from_payload(payload: Mapping[str, Any]) -> DirectiveInfo | None:
    """Extract the maintainer directive the webhook handler stashed, if any."""
    raw = payload.get("_robomp_directive")
    if not isinstance(raw, Mapping):
        return None
    body = raw.get("body")
    author = raw.get("author")
    if not isinstance(body, str) or not body.strip():
        return None
    if not isinstance(author, str) or not author.strip():
        return None
    pragmas: list[tuple[str, str]] = []
    raw_pragmas = raw.get("pragmas")
    if isinstance(raw_pragmas, list):
        for entry in raw_pragmas:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                k, v = entry
                if isinstance(k, str) and isinstance(v, str):
                    pragmas.append((k, v))
    return DirectiveInfo(
        body=body,
        author=author,
        pragmas=tuple(pragmas),
        authorizes_impl=bool(raw.get("authorizes_impl")),
    )


async def _post_pr_review_started_comment(
    *,
    db: Database,
    github: GitHubBackend,
    key: str,
    repo_full: str,
    pr_number: int,
    labels: frozenset[str],
    head_sha: str,
    allowed_labels: frozenset[str],
    review_focus: PrReviewFocus,
) -> None:
    operation_key = f"post_pr_review_started_comment:{key}:{head_sha}"
    if not db.reserve_side_effect(operation_key):
        succeeded = db.side_effect_succeeded(operation_key)
        message = "side effect already succeeded: %s" if succeeded else "side effect already pending: %s"
        log.info(message, operation_key, extra={"key": key, "operation_key": operation_key, "succeeded": succeeded})
        return
    trigger = sorted(matching_review_labels(labels, allowed_labels))
    trigger_text = f" triggered by `{trigger[0]}`" if trigger else ""
    if review_focus.mode == "verify-fixes":
        detail = (
            "verifying fixes for my prior requested changes"
            if review_focus.prior_review_state == "CHANGES_REQUESTED"
            else "re-reviewing the changes since my last review"
        )
        body = (
            f"Robo-MS is {detail} on this PR now{trigger_text}. "
            "I’ll post an `APPROVE` or `REQUEST_CHANGES` review when the eval finishes."
        )
    else:
        body = (
            f"Robo-MS is reviewing this PR now{trigger_text}. "
            "I’ll post an `APPROVE` or `REQUEST_CHANGES` review when the eval finishes."
        )
    try:
        comment = await github.post_comment(repo_full, pr_number, body)
    except GitHubError as exc:
        db.mark_side_effect_failed(operation_key, str(exc))
        db.log_tool_call(
            issue_key=key,
            tool=operation_key,
            args={
                "repo": repo_full,
                "pr": pr_number,
                "head_sha": head_sha,
                "review_focus_mode": review_focus.mode,
                "prior_review_commit_id": review_focus.prior_review_commit_id,
                "prior_review_id": review_focus.prior_review_id,
            },
            error=str(exc),
        )
        log.warning("review-start comment failed", extra={"repo": repo_full, "pr": pr_number, "err": str(exc)})
        return
    db.mark_side_effect_succeeded(operation_key)
    db.log_tool_call(
        issue_key=key,
        tool=operation_key,
        args={
            "repo": repo_full,
            "pr": pr_number,
            "head_sha": head_sha,
            "review_focus_mode": review_focus.mode,
            "prior_review_commit_id": review_focus.prior_review_commit_id,
            "prior_review_id": review_focus.prior_review_id,
        },
        result={"comment_id": comment.id},
    )


def _ci_gate_notice_body(ci: PullRequestCiStatusInfo) -> str:
    return (
        "Robo-MS won’t review this PR until all CI checks pass "
        f"({ci.failed_count} failing, {ci.pending_count} pending of {ci.total_count} checks). "
        "I’ll review automatically once the checks are green — push fixes or re-run CI and I’ll pick it up."
    )


async def _apply_pr_review_ci_gate(
    *,
    settings: Settings,
    db: Database,
    github: GitHubBackend,
    key: str,
    repo_full: str,
    pr_number: int,
    head_sha: str,
    ci: PullRequestCiStatusInfo,
    received_at: str | None,
) -> TaskOutcome | None:
    """Evaluate the CI gate for the live head, advance per-PR gate state, and
    post/refresh the single per-episode notice. Returns the gate outcome:
    ``None`` => CI passed (proceed to review); skipped/deferred otherwise.

    Shared by ``review_pr`` and the cheap ``probe_pr_review_ci`` task so both
    paths share one idempotent, episode-scoped notification."""
    outcome = _pr_review_ci_gate_outcome(settings, ci, received_at=received_at)
    signature = _ci_gate_summary(ci)
    if outcome is None:
        db.transition_pr_review_gate(
            issue_key=key,
            repo=repo_full,
            pr_number=pr_number,
            head_sha=head_sha,
            gate_status="passed",
            ci_signature=signature,
        )
        return None
    gate_status = "blocked" if outcome.state == "skipped" else "pending"
    transition = db.transition_pr_review_gate(
        issue_key=key,
        repo=repo_full,
        pr_number=pr_number,
        head_sha=head_sha,
        gate_status=gate_status,
        ci_signature=signature,
    )
    log.info(
        "PR CI gate blocked review",
        extra={
            "repo": repo_full,
            "pr": pr_number,
            "state": ci.state,
            "total": ci.total_count,
            "pending": ci.pending_count,
            "failed": ci.failed_count,
            "gate_status": gate_status,
            "episode": transition.episode,
            "notify": transition.should_notify,
        },
    )
    if gate_status == "blocked":
        await _maybe_post_pr_review_ci_gate_notice(
            settings=settings,
            db=db,
            github=github,
            key=key,
            repo_full=repo_full,
            pr_number=pr_number,
            head_sha=head_sha,
            ci=ci,
            transition=transition,
        )
    return outcome


async def _maybe_post_pr_review_ci_gate_notice(
    *,
    settings: Settings,
    db: Database,
    github: GitHubBackend,
    key: str,
    repo_full: str,
    pr_number: int,
    head_sha: str,
    ci: PullRequestCiStatusInfo,
    transition: PrReviewGateTransition,
) -> None:
    """Post exactly one gate notice per blocking episode, then (optionally) edit
    that same comment in place as CI counts change. Idempotent across heads and
    reconciler cycles: the create is keyed on the episode, and a sticky edit only
    fires when the CI signature changed within an already-announced episode."""
    body = _ci_gate_notice_body(ci)
    if transition.should_notify:
        operation_key = f"post_pr_review_ci_gate_notice:{key}:episode:{transition.episode}"
        if not db.reserve_side_effect(operation_key):
            return
        try:
            comment = await github.post_comment(repo_full, pr_number, body)
        except GitHubError as exc:
            db.mark_side_effect_failed(operation_key, str(exc))
            db.log_tool_call(
                issue_key=key,
                tool=operation_key,
                args={"repo": repo_full, "pr": pr_number, "head_sha": head_sha, "ci_state": ci.state, "episode": transition.episode},
                error=str(exc),
            )
            log.warning("CI gate comment failed", extra={"repo": repo_full, "pr": pr_number, "err": str(exc)})
            return
        db.mark_side_effect_succeeded(operation_key)
        db.mark_pr_review_gate_notified(key, episode=transition.episode, comment_id=comment.id)
        db.log_tool_call(
            issue_key=key,
            tool=operation_key,
            args={"repo": repo_full, "pr": pr_number, "head_sha": head_sha, "ci_state": ci.state, "episode": transition.episode},
            result={"comment_id": comment.id, "episode": transition.episode},
        )
        return
    comment_id = transition.state.last_notice_comment_id
    if not (
        settings.pr_review_gate_sticky_comment_enabled
        and transition.signature_changed
        and comment_id is not None
    ):
        return
    operation_key = f"update_pr_review_ci_gate_notice:{key}:episode:{transition.episode}:{_ci_gate_summary(ci)}"
    if not db.reserve_side_effect(operation_key):
        return
    try:
        await github.update_comment(repo_full, comment_id, body)
    except GitHubError as exc:
        db.mark_side_effect_failed(operation_key, str(exc))
        db.log_tool_call(
            issue_key=key,
            tool=operation_key,
            args={"repo": repo_full, "pr": pr_number, "comment_id": comment_id, "ci_state": ci.state},
            error=str(exc),
        )
        log.warning("CI gate comment edit failed", extra={"repo": repo_full, "pr": pr_number, "err": str(exc)})
        return
    db.mark_side_effect_succeeded(operation_key)
    db.log_tool_call(
        issue_key=key,
        tool=operation_key,
        args={"repo": repo_full, "pr": pr_number, "comment_id": comment_id, "ci_state": ci.state},
        result={"comment_id": comment_id, "episode": transition.episode},
    )


async def _fetch_thread(
    github: GitHubBackend,
    repo: str,
    number: int,
    *,
    is_pr: bool,
) -> tuple[ThreadMessage, ...]:
    """Pull the full conversation thread (body + comments + reviews) for `number`.

    Best-effort: any sub-fetch that fails is logged + dropped so a stale
    review-comments endpoint doesn't block the directive from running.
    """
    messages: list[ThreadMessage] = []

    async def fetch_body() -> None:
        try:
            item = await github.get_issue(repo, number)
        except GitHubError as exc:
            log.warning("thread body fetch failed", extra={"repo": repo, "n": number, "err": str(exc)})
            return
        if item.body and item.body.strip():
            messages.append(
                ThreadMessage(
                    kind="pr_body" if is_pr else "issue_body",
                    author=item.author or "",
                    body=item.body,
                    created_at="",  # not exposed by IssueInfo
                )
            )

    async def fetch_comments() -> None:
        try:
            comments = await github.list_comments(repo, number)
        except GitHubError as exc:
            log.warning("thread comments fetch failed", extra={"err": str(exc)})
            return
        for c in comments:
            messages.append(
                ThreadMessage(
                    kind="comment",
                    author=c.author,
                    body=c.body,
                    created_at=c.created_at,
                )
            )

    async def fetch_review_comments() -> None:
        try:
            review_comments = await github.list_review_comments(repo, number)
        except GitHubError as exc:
            log.warning("thread review-comments fetch failed", extra={"err": str(exc)})
            return
        for r in review_comments:
            messages.append(
                ThreadMessage(
                    kind="review_comment",
                    author=r.author,
                    body=r.body,
                    created_at=r.created_at,
                    path=r.path,
                    line=r.line,
                )
            )

    async def fetch_reviews() -> None:
        try:
            reviews = await github.list_pr_reviews(repo, number)
        except GitHubError as exc:
            log.warning("thread reviews fetch failed", extra={"err": str(exc)})
            return
        for rv in reviews:
            messages.append(
                ThreadMessage(
                    kind="review",
                    author=rv.author,
                    body=rv.body,
                    created_at=rv.submitted_at,
                    state=rv.state,
                )
            )

    calls = [fetch_body(), fetch_comments()]
    if is_pr:
        calls.extend([fetch_review_comments(), fetch_reviews()])
    await asyncio.gather(*calls)

    # ISO 8601 strings sort chronologically. Body has no timestamp so it
    # sorts first (empty string < any "2026-…" string).
    messages.sort(key=lambda m: m.created_at or "")
    return tuple(messages)


async def _attach_thread(
    github: GitHubBackend,
    directive: DirectiveInfo | None,
    repo: str,
    number: int,
    *,
    is_pr: bool,
) -> DirectiveInfo | None:
    """Hydrate a directive with the live conversation thread (or no-op if None)."""
    if directive is None:
        return None
    thread = await _fetch_thread(github, repo, number, is_pr=is_pr)
    return replace(directive, thread=thread)


async def _resolve_repo_and_issue(
    github: GitHubBackend,
    payload: Mapping[str, Any],
) -> tuple[RepoInfo, IssueInfo]:
    repo, issue = parse_issue_payload(payload)
    if not issue.body:
        # Webhook payloads sometimes omit body; refetch to be safe.
        try:
            issue = await github.get_issue(repo.full_name, issue.number)
        except GitHubError as exc:
            log.warning("issue refetch failed", extra={"err": str(exc)})
    return repo, issue


async def _resolve_issue_row_for_pr(
    *,
    db: Database,
    github: GitHubBackend,
    repo_full: str,
    pr_number: int,
) -> tuple[IssueRow | None, PullRequestInfo | None]:
    """Find the originating issue row for a PR, repairing stale mappings when possible."""
    issue_row = db.find_issue_by_pr(repo_full, pr_number)
    pr_info: PullRequestInfo | None = None
    if issue_row is None or issue_row.branch is None:
        try:
            pr_info = await github.get_pull_request(repo_full, pr_number)
        except GitHubError as exc:
            log.warning("PR metadata fetch failed", extra={"repo": repo_full, "pr": pr_number, "err": str(exc)})
            if issue_row is None:
                raise _github_fetch_failed(exc) from exc
            return issue_row, None

    if issue_row is None and pr_info is not None and pr_info.head_ref:
        issue_row = db.find_issue_by_branch(repo_full, pr_info.head_ref)
        if issue_row is not None:
            db.set_issue_pr(issue_row.key, pr_number)
            issue_row = db.get_issue(issue_row.key) or issue_row
    elif issue_row is not None and issue_row.branch is None and pr_info is not None and pr_info.head_ref:
        db.set_issue_branch(issue_row.key, pr_info.head_ref)
        issue_row = db.get_issue(issue_row.key) or issue_row
    return issue_row, pr_info


def _can_handle_pr_directly(*, settings: Settings, repo_full: str, pr: PullRequestInfo) -> bool:
    """Only bot-owned same-repo PR branches are safe to amend directly."""
    return _direct_pr_skip_reason(settings=settings, repo_full=repo_full, pr=pr) is None


async def triage_issue(
    *,
    settings: Settings,
    db: Database,
    github: GitHubBackend,
    sandbox: SandboxManager,
    git_transport: GitTransport,
    payload: Mapping[str, Any],
    delivery_id: str,
    attempts: int = 0,
    slot_uid: int | None = None,
) -> TaskOutcome | None:
    repo, issue = await _resolve_repo_and_issue(github, payload)
    if issue.is_pull_request:
        log.info("skip: triage on PR-like issue", extra={"repo": repo.full_name, "n": issue.number})
        return _skipped("skip: triage on PR-like issue")
    key = issue_key(repo.full_name, issue.number)
    if db.get_issue(key) is None:
        # First-time triage: bail if a PR (human or another bot) already
        # claims to close this issue via Closes/Fixes/Resolves syntax or
        # the Development panel. We never replay closing-PR detection on
        # a follow-up because by then the bot has already committed
        # resources (workspace, omp session) to this issue.
        try:
            closing_prs = await github.list_closing_pull_requests(repo.full_name, issue.number)
        except GitHubError as exc:
            # Fail-open: a transient timeline fetch failure shouldn't
            # block legitimate triage. Worst case we do redundant work.
            log.warning(
                "closing-PR check failed; proceeding with triage",
                extra={"key": key, "err": str(exc)},
            )
            closing_prs = ()
        if closing_prs:
            log.info(
                "skip: issue already covered by an open PR",
                extra={"key": key, "prs": list(closing_prs)},
            )
            return _skipped("skip: issue already covered by an open PR")
    db.upsert_issue(key=key, repo=repo.full_name, number=issue.number, state="reproducing")
    clone_url = repo.clone_url
    workspace = sandbox.ensure_workspace(
        repo=repo.full_name,
        number=issue.number,
        title=issue.title,
        clone_url=clone_url,
        default_branch=repo.default_branch,
        author_name=settings.resolved_author_name,
        author_email=settings.git_author_email,
        slot_uid=slot_uid,
    )
    db.upsert_issue(
        key=key,
        repo=repo.full_name,
        number=issue.number,
        state="reproducing",
        branch=workspace.branch,
        session_dir=str(workspace.session_dir),
    )
    inputs = TaskInputs(
        settings=settings,
        db=db,
        github=github,
        git_transport=git_transport,
        repo=repo,
        issue=issue,
        workspace=workspace,
        delivery_id=delivery_id,
        attempts=attempts,
        slot_uid=slot_uid,
        natives_cache=sandbox.natives_cache,
    )
    await run_task(task_kind="triage_issue", inputs=inputs)


def _pr_review_focus(latest_review: PullRequestReviewInfo | None, head_sha: str) -> PrReviewFocus:
    if latest_review is None:
        return PrReviewFocus()
    state = latest_review.state.upper()
    prior = latest_review.commit_id or ""
    # CHANGES_REQUESTED: its blocking findings must be verified, so re-review in verify-fixes
    # even when the prior commit is unknown (the agent verifies against the full diff).
    if state == "CHANGES_REQUESTED" and prior != head_sha:
        reason = (
            "verify fixes for prior bot CHANGES_REQUESTED review"
            if prior
            else "verify fixes for prior bot CHANGES_REQUESTED review with unknown commit"
        )
        return PrReviewFocus(
            mode="verify-fixes",
            reason=reason,
            prior_review_state="CHANGES_REQUESTED",
            prior_review_commit_id=prior,
            prior_review_id=latest_review.id,
            prior_review_submitted_at=latest_review.submitted_at,
        )
    # APPROVE/COMMENT: non-blocking. Re-review only the incremental delta, which needs a known
    # prior commit. A transient-failure COMMENT is not a real verdict and never anchors.
    if (
        state in {"APPROVED", "COMMENTED"}
        and prior
        and prior != head_sha
        and not (state == "COMMENTED" and pr_review_comment_retryable(latest_review.body))
    ):
        return PrReviewFocus(
            mode="verify-fixes",
            reason=f"incremental re-review of delta since prior bot {state} review",
            prior_review_state=state,
            prior_review_commit_id=prior,
            prior_review_id=latest_review.id,
            prior_review_submitted_at=latest_review.submitted_at,
        )
    return PrReviewFocus()

def _run_workspace_git(workspace: Workspace, cmd: Sequence[str], *, timeout: float | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        cwd=str(workspace.repo_dir),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


async def review_pr(
    *,
    settings: Settings,
    db: Database,
    github: GitHubBackend,
    sandbox: SandboxManager,
    git_transport: GitTransport,
    payload: Mapping[str, Any],
    delivery_id: str,
    attempts: int = 0,
    received_at: str | None = None,
    slot_uid: int | None = None,
) -> TaskOutcome | None:
    pr_node = payload.get("pull_request") or {}
    pr_number = int(pr_node.get("number") or 0)
    repo_full = repo_full_name(payload) or ""
    if pr_number <= 0 or not repo_full:
        log.info("skip: review_pr missing repo/number")
        return _skipped("skip: review_pr missing repo/number")
    try:
        repo, issue, pr = await asyncio.gather(
            github.get_repo(repo_full),
            github.get_issue(repo_full, pr_number),
            github.get_pull_request(repo_full, pr_number),
        )
    except GitHubError as exc:
        log.warning("review_pr fetch failed", extra={"repo": repo_full, "pr": pr_number, "err": str(exc)})
        raise _github_fetch_failed(exc) from exc

    labels = normalize_label_names(issue.labels)
    allowed_labels = settings.pr_review_label_allowlist
    if allowed_labels and labels.isdisjoint(allowed_labels):
        log.info("skip: PR missing review trigger label", extra={"repo": repo_full, "pr": pr_number, "labels": sorted(labels)})
        return _skipped("skip: PR missing review trigger label")
    if pr.state.lower() != "open":
        log.info("skip: PR not open", extra={"repo": repo_full, "pr": pr_number, "state": pr.state})
        return _skipped("skip: PR not open")
    if pr.draft:
        log.info("skip: PR is draft", extra={"repo": repo_full, "pr": pr_number})
        return _skipped("skip: PR is draft")
    if pr.author.endswith("[bot]") or pr.author_type == "Bot":
        log.info("skip: PR authored by bot", extra={"repo": repo_full, "pr": pr_number, "author": pr.author})
        return _skipped("skip: PR authored by bot")
    key = issue_key(repo.full_name, pr_number)
    expected_head = _expected_head_sha(payload)
    if expected_head and expected_head != pr.head_sha:
        log.info(
            "skip: review_pr head superseded since event was queued",
            extra={"repo": repo_full, "pr": pr_number, "expected_head": expected_head, "head_sha": pr.head_sha},
        )
        return _skipped("skip: PR head superseded since event was queued")
    if settings.pr_review_ci_gate_enabled:
        try:
            ci_status = await github.get_commit_ci_status(repo.full_name, pr.head_sha)
        except GitHubError as exc:
            log.warning("PR CI status fetch failed", extra={"repo": repo.full_name, "pr": pr_number, "err": str(exc)})
            raise _github_fetch_failed(exc) from exc
        ci_outcome = await _apply_pr_review_ci_gate(
            settings=settings,
            db=db,
            github=github,
            key=key,
            repo_full=repo_full,
            pr_number=pr_number,
            head_sha=pr.head_sha,
            ci=ci_status,
            received_at=received_at,
        )
        if ci_outcome is not None:
            return ci_outcome
    review_labeled = "triaged" in labels or any(label.startswith("review:") for label in labels)
    try:
        reviews = await github.list_pr_reviews(repo_full, pr_number)
    except GitHubError as exc:
        log.warning("skip: PR review history fetch failed", extra={"repo": repo_full, "pr": pr_number, "err": str(exc)})
        raise _github_fetch_failed(exc) from exc
    bot_reviews = [
        review
        for review in reviews
        if review.author.lower() == settings.bot_login.lower()
        and review.state.upper() in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}
    ]
    latest_review = max(bot_reviews, key=lambda review: review.submitted_at) if bot_reviews else None
    if latest_review is None:
        if db.has_completed_pr_review(repo_full, pr_number, pr.head_sha):
            log.info("skip: PR review already submitted", extra={"repo": repo_full, "pr": pr_number})
            return _skipped("skip: PR review already submitted")
    else:
        latest_state = latest_review.state.upper()
        if latest_state in {"APPROVED", "COMMENTED"}:
            if latest_review.commit_id == pr.head_sha:
                if latest_state == "COMMENTED" and pr_review_comment_retryable(latest_review.body):
                    log.info("retrying PR review after retryable comment-only failure", extra={"repo": repo_full, "pr": pr_number})
                else:
                    reason = "skip: PR already approved by bot" if latest_state == "APPROVED" else "skip: PR already commented by bot"
                    log.info(reason, extra={"repo": repo_full, "pr": pr_number})
                    return _skipped(reason)
            else:
                log.info(
                    "reviewing PR after prior review on different or unknown head",
                    extra={
                        "repo": repo_full,
                        "pr": pr_number,
                        "review_commit": latest_review.commit_id,
                        "head_sha": pr.head_sha,
                    },
                )
        if latest_state == "CHANGES_REQUESTED":
            if latest_review.commit_id == pr.head_sha:
                log.info(
                    "skip: PR changes already requested for current head",
                    extra={
                        "repo": repo_full,
                        "pr": pr_number,
                        "review_commit": latest_review.commit_id,
                        "head_sha": pr.head_sha,
                    },
                )
                return _skipped("skip: PR changes already requested for current head")
            log.info(
                "reviewing PR after prior review on different or unknown head",
                extra={
                    "repo": repo_full,
                    "pr": pr_number,
                    "review_commit": latest_review.commit_id,
                    "head_sha": pr.head_sha,
                },
            )
    review_focus = _pr_review_focus(latest_review, pr.head_sha)
    if review_labeled:
        log.info(
            "review labels present without submitted review; retrying",
            extra={"repo": repo_full, "pr": pr_number, "labels": sorted(labels)},
        )

    try:
        pr_files = await github.list_pr_files(repo.full_name, pr_number)
    except GitHubError as exc:
        log.warning("PR file list fetch failed", extra={"repo": repo.full_name, "pr": pr_number, "err": str(exc)})
        raise
    if not pr_files:
        raise RuntimeError("PR file list is empty")
    changed_paths = tuple(
        path
        for file in pr_files
        for path in (file.path, file.previous_filename)
        if path
    )

    # Git worktree prep fetches the (large) monorepo through the proxy with a
    # synchronous client; run it off the event loop so the HTTP server (health,
    # webhook receipt) stays responsive while reviews run. run_task already does
    # the same for the agent RPC.
    workspace = await asyncio.to_thread(
        sandbox.ensure_workspace,
        repo=repo.full_name,
        number=pr_number,
        title=issue.title,
        clone_url=repo.clone_url,
        default_branch=repo.default_branch,
        pr_head=pr_number,
        pr_base_ref=pr.base_ref,
        pr_changed_paths=changed_paths,
        pr_head_sha=pr.head_sha,
        author_name=settings.resolved_author_name,
        author_email=settings.git_author_email,
        slot_uid=slot_uid,
    )

    # Reap superseded PR-head workspaces for this PR. claim_next_event()
    # serializes queued/running rows by issue_key, so no other event for this
    # PR runs concurrently; only older heads remain to remove.
    # Best-effort: cleanup of stale-head workspaces must never fail the review of
    # the current head (a `git worktree remove` can time out under disk/IO load).
    try:
        removed = await asyncio.to_thread(
            sandbox.remove_superseded_pr_review_workspaces,
            repo=repo.full_name,
            number=pr_number,
            keep_head_sha=pr.head_sha,
        )
    except Exception as exc:  # noqa: BLE001 - stale-head cleanup is non-critical
        log.warning(
            "pr_review superseded workspace cleanup failed (non-fatal)",
            extra={"repo": repo.full_name, "pr": pr_number, "head_sha": pr.head_sha, "err": str(exc)},
        )
    else:
        if removed > 0:
            log.info(
                "pr_review superseded workspaces removed",
                extra={"repo": repo.full_name, "pr": pr_number, "removed": removed, "head_sha": pr.head_sha},
            )

    if (
        review_focus.mode == "verify-fixes"
        and review_focus.prior_review_state == "CHANGES_REQUESTED"
        and latest_review is not None
        and latest_review.commit_id
    ):
        posted_findings = db.list_pr_review_posted_findings(repo.full_name, pr_number)
        prior_review_id = latest_review.id
        blocking_findings = tuple(
            finding
            for finding in posted_findings
            if str(finding.review_id) == str(prior_review_id) and finding.severity in {"critical", "required"}
        )
        if not blocking_findings:
            log.info(
                "accepted-suggestion fast path ineligible: no_recorded_blocking_suggestions",
                extra={"repo": repo.full_name, "pr": pr_number, "prior_review_id": prior_review_id},
            )
        elif any(not finding.suggestion_replacement for finding in blocking_findings):
            log.info(
                "accepted-suggestion fast path ineligible: prior_review_has_non_suggestion_blocker",
                extra={"repo": repo.full_name, "pr": pr_number, "prior_review_id": prior_review_id},
            )
        else:
            prior_verify = await asyncio.to_thread(
                _run_workspace_git,
                workspace,
                ["git", "rev-parse", "--verify", "--quiet", latest_review.commit_id],
                timeout=30.0,
            )
            if prior_verify.returncode != 0:
                log.info(
                    "accepted-suggestion fast path ineligible: prior review commit unavailable",
                    extra={
                        "repo": repo.full_name,
                        "pr": pr_number,
                        "prior_review_id": prior_review_id,
                        "prior_review_commit_id": latest_review.commit_id,
                    },
                )
            else:
                delta = await asyncio.to_thread(
                    _run_workspace_git,
                    workspace,
                    ["git", "diff", "--no-color", f"{latest_review.commit_id}..{pr.head_sha}", "--", *changed_paths],
                    timeout=60.0,
                )
                if delta.returncode != 0:
                    log.info(
                        "accepted-suggestion fast path ineligible: delta diff failed",
                        extra={
                            "repo": repo.full_name,
                            "pr": pr_number,
                            "prior_review_id": prior_review_id,
                            "prior_review_commit_id": latest_review.commit_id,
                        },
                    )
                else:
                    result = accepted_suggestions_fast_path_result(
                        latest_review=latest_review,
                        current_head_sha=pr.head_sha,
                        posted_findings=posted_findings,
                        delta_diff=delta.stdout,
                        terminal_events_enabled=settings.pr_review_terminal_events,
                        bot_login=settings.bot_login,
                        pr_author=pr.author,
                    )
                    if result.eligible:
                        operation_key = (
                            f"pr_review_accepted_suggestions_fast_path:{key}:{pr.head_sha}:{latest_review.id}"
                        )
                        if not db.reserve_side_effect(operation_key):
                            if db.side_effect_succeeded(operation_key):
                                return TaskOutcome(
                                    "skipped",
                                    "accepted-suggestion fast path already submitted for current head",
                                )
                            return TaskOutcome(
                                "skipped",
                                "accepted-suggestion fast path already pending for current head",
                            )
                        prior_short = latest_review.commit_id[:7]
                        head_short = pr.head_sha[:7]
                        count = len(result.suggestions)
                        body = (
                            f"Robo-MS verified that all {count} prior GitHub suggested change(s) from my "
                            f"requested-changes review {latest_review.id} were applied exactly in "
                            f"`{prior_short}..{head_short}`; skipping the verify-fixes agent."
                        )
                        event = result.event or "COMMENT"
                        if event == "COMMENT":
                            body = (
                                body
                                + "\n\nTerminal review events are disabled or self-review is disallowed, so this is a non-blocking status comment."
                            )
                        try:
                            review = await github.submit_pr_review(
                                repo=repo.full_name,
                                pr_number=pr_number,
                                body=body,
                                event=event,
                                comments=[],
                                commit_id=pr.head_sha,
                            )
                        except GitHubError as exc:
                            db.mark_side_effect_failed(operation_key, str(exc))
                            db.log_tool_call(
                                issue_key=key,
                                tool="pr_review_accepted_suggestions_fast_path",
                                args={
                                    "repo": repo.full_name,
                                    "pr": pr_number,
                                    "head_sha": pr.head_sha,
                                    "prior_review_id": latest_review.id,
                                    "prior_review_commit_id": latest_review.commit_id,
                                },
                                error=str(exc),
                            )
                            raise _github_fetch_failed(exc) from exc
                        db.mark_side_effect_succeeded(operation_key)
                        db.record_pr_review_completed_review(
                            issue_key=key,
                            repo=repo.full_name,
                            pr_number=pr_number,
                            head_sha=pr.head_sha,
                            github_review_id=review.id,
                            event=event,
                        )
                        db.transition_pr_review_gate(
                            issue_key=key,
                            repo=repo.full_name,
                            pr_number=pr_number,
                            head_sha=pr.head_sha,
                            gate_status="reviewed",
                        )
                        db.upsert_issue(
                            key=key,
                            repo=repo.full_name,
                            number=pr_number,
                            state="reviewing",
                            branch=workspace.branch,
                            session_dir=str(workspace.session_dir),
                            pr_number=pr_number,
                            review_head_sha=pr.head_sha,
                        )
                        db.log_tool_call(
                            issue_key=key,
                            tool="pr_review_accepted_suggestions_fast_path",
                            args={
                                "repo": repo.full_name,
                                "pr": pr_number,
                                "head_sha": pr.head_sha,
                                "prior_review_id": latest_review.id,
                                "prior_review_commit_id": latest_review.commit_id,
                            },
                            result={
                                "event": event,
                                "suggestion_count": count,
                                "comment_ids": [s.comment_id for s in result.suggestions],
                                "reason": result.reason,
                            },
                        )
                        return TaskOutcome(
                            "done",
                            "accepted prior GitHub suggestions exactly; skipped verify-fixes agent",
                        )
                    log.info(
                        "accepted-suggestion fast path ineligible: %s",
                        result.reason,
                        extra={
                            "repo": repo.full_name,
                            "pr": pr_number,
                            "prior_review_id": prior_review_id,
                            "reason": result.reason,
                            "extra_delta_paths": result.extra_delta_paths,
                        },
                    )

    if settings.pr_review_started_comments_enabled:
        await _post_pr_review_started_comment(
            db=db,
            github=github,
            key=key,
            repo_full=repo_full,
            pr_number=pr_number,
            labels=labels,
            head_sha=pr.head_sha,
            allowed_labels=allowed_labels,
            review_focus=review_focus,
        )
    db.upsert_issue(
        key=key,
        repo=repo.full_name,
        number=pr_number,
        state="reviewing",
        branch=workspace.branch,
        session_dir=str(workspace.session_dir),
        pr_number=pr_number,
        review_head_sha=pr.head_sha,
    )
    inputs = TaskInputs(
        settings=settings,
        db=db,
        github=github,
        git_transport=git_transport,
        repo=repo,
        issue=issue,
        workspace=workspace,
        delivery_id=delivery_id,
        attempts=attempts,
        slot_uid=slot_uid,
        natives_cache=sandbox.natives_cache,
        pr_review_focus=review_focus,
    )
    await run_task(task_kind="review_pr", inputs=inputs, pr_number=pr_number, pr=pr)


def _enqueue_review_for_head(db: Database, repo_full: str, pr: PullRequestInfo) -> bool:
    """Queue a full ``review_pr`` for ``pr``'s current head after CI passed.
    Idempotent per head via the delivery id, so a webhook-driven review for the
    same head is never duplicated into a second agent run."""
    delivery_id = f"ci-probe-review-{repo_full.replace('/', '__')}-{pr.number}-{pr.head_sha}"
    payload = {
        "action": "synchronize",
        "repository": {"full_name": repo_full},
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
    return db.record_event(
        delivery_id=delivery_id,
        event_type="pull_request",
        repo=repo_full,
        issue_key=issue_key(repo_full, pr.number),
        payload=payload,
        state="queued",
        task="review_pr",
        route_reason="ci probe: CI passed",
        route_version=1,
    )


async def probe_pr_review_ci(
    *,
    settings: Settings,
    db: Database,
    github: GitHubBackend,
    payload: Mapping[str, Any],
    delivery_id: str,
    received_at: str | None = None,
    event_issue_key: str | None = None,
) -> TaskOutcome | None:
    """Cheap CI-gate probe: confirm eligibility + current head, evaluate the CI
    gate (advancing per-PR gate state and posting at most one episode notice),
    and enqueue a full ``review_pr`` only once CI is green. Never builds a
    workspace or runs the review agent, so red/pending heads cost a few reads
    instead of a worker spin-up. Shared by check-webhook routing and the
    reconciler backstop."""
    # CI/check webhook payloads (check_suite/check_run/status) carry no
    # ``pull_request.number``; the router resolves it into the event's issue_key,
    # so prefer that and fall back to the payload for PR-shaped events.
    if event_issue_key:
        repo_full, _, num = event_issue_key.rpartition("#")
        pr_number = int(num) if num.isdigit() else 0
    else:
        pr_node = payload.get("pull_request") or {}
        pr_number = int(pr_node.get("number") or 0)
        repo_full = repo_full_name(payload) or ""
    if pr_number <= 0 or not repo_full:
        return _skipped("skip: probe missing repo/number")
    key = issue_key(repo_full, pr_number)
    # Debounce a burst of CI webhooks for the same head before any GitHub fetch:
    # if this head was gate-checked within the window, no-op. A new head (or a
    # reconciler re-probe, which is interval >> debounce) still falls through.
    if settings.pr_review_ci_gate_enabled:
        event_head = _event_head_sha(payload)
        if event_head:
            gate = db.get_pr_review_gate_state(key)
            if gate is not None and gate.current_head_sha == event_head:
                elapsed = _elapsed_since_iso(gate.last_checked_at)
                if elapsed is not None and elapsed < settings.pr_review_ci_debounce_seconds:
                    return _skipped("skip: CI probe debounced (head checked recently)")
    try:
        issue, pr = await asyncio.gather(
            github.get_issue(repo_full, pr_number),
            github.get_pull_request(repo_full, pr_number),
        )
    except GitHubError as exc:
        log.warning("probe_pr_review_ci fetch failed", extra={"repo": repo_full, "pr": pr_number, "err": str(exc)})
        raise _github_fetch_failed(exc) from exc
    if pr.state.lower() != "open":
        return _skipped("skip: PR not open")
    if pr.draft:
        return _skipped("skip: PR is draft")
    if pr.author.endswith("[bot]") or pr.author_type == "Bot":
        return _skipped("skip: PR authored by bot")
    labels = normalize_label_names(issue.labels)
    allowed_labels = settings.pr_review_label_allowlist
    if allowed_labels and labels.isdisjoint(allowed_labels):
        return _skipped("skip: PR missing review trigger label")
    expected_head = _expected_head_sha(payload)
    if expected_head and expected_head != pr.head_sha:
        return _skipped("skip: PR head superseded since probe was queued")
    if db.has_completed_pr_review(repo_full, pr_number, pr.head_sha):
        db.transition_pr_review_gate(
            issue_key=key, repo=repo_full, pr_number=pr_number,
            head_sha=pr.head_sha, gate_status="reviewed",
        )
        return _skipped("skip: PR review already submitted for head")
    if not settings.pr_review_ci_gate_enabled:
        enqueued = _enqueue_review_for_head(db, repo_full, pr)
        return TaskOutcome("done", "CI gate disabled; review enqueued" if enqueued else "review already queued")
    try:
        ci_status = await github.get_commit_ci_status(repo_full, pr.head_sha)
    except GitHubError as exc:
        log.warning("probe CI status fetch failed", extra={"repo": repo_full, "pr": pr_number, "err": str(exc)})
        raise _github_fetch_failed(exc) from exc
    ci_outcome = await _apply_pr_review_ci_gate(
        settings=settings,
        db=db,
        github=github,
        key=key,
        repo_full=repo_full,
        pr_number=pr_number,
        head_sha=pr.head_sha,
        ci=ci_status,
        received_at=received_at,
    )
    if ci_outcome is not None:
        # blocked (skipped, episode notice handled) or pending (queued -> the
        # queue re-runs this probe after the retry delay; backstop for missed
        # check webhooks). Either way, no review is enqueued yet.
        return ci_outcome
    enqueued = _enqueue_review_for_head(db, repo_full, pr)
    return TaskOutcome("done", "CI passed; review enqueued" if enqueued else "CI passed; review already queued")


async def handle_comment(
    *,
    settings: Settings,
    db: Database,
    github: GitHubBackend,
    sandbox: SandboxManager,
    git_transport: GitTransport,
    payload: Mapping[str, Any],
    delivery_id: str,
    attempts: int = 0,
    slot_uid: int | None = None,
) -> TaskOutcome | None:
    repo, issue = await _resolve_repo_and_issue(github, payload)
    key = issue_key(repo.full_name, issue.number)
    existing = db.get_issue(key)
    directive = _directive_from_payload(payload)
    comment = _comment_from_payload(payload)
    clone_url = repo.clone_url

    if existing is None:
        if directive is None:
            log.info("skip: comment on unknown issue", extra={"key": key})
            return _skipped("skip: comment on unknown issue")
        # Maintainer summon on an untriaged issue: bootstrap a row + workspace,
        # then route through triage-with-directive so the agent classifies
        # first and executes the directive in the same RPC turn.
        log.info("directive bootstrap", extra={"key": key, "author": directive.author})
        db.upsert_issue(key=key, repo=repo.full_name, number=issue.number, state="reproducing")
        workspace = sandbox.ensure_workspace(
            repo=repo.full_name,
            number=issue.number,
            title=issue.title,
            clone_url=clone_url,
            default_branch=repo.default_branch,
            author_name=settings.resolved_author_name,
            author_email=settings.git_author_email,
            slot_uid=slot_uid,
        )
        db.upsert_issue(
            key=key,
            repo=repo.full_name,
            number=issue.number,
            state="reproducing",
            branch=workspace.branch,
            session_dir=str(workspace.session_dir),
        )
        inputs = TaskInputs(
            settings=settings,
            db=db,
            github=github,
            git_transport=git_transport,
            repo=repo,
            issue=issue,
            workspace=workspace,
            delivery_id=delivery_id,
            attempts=attempts,
            slot_uid=slot_uid,
            natives_cache=sandbox.natives_cache,
        )
        directive = await _attach_thread(github, directive, repo.full_name, issue.number, is_pr=False)
        await run_task(task_kind="triage_issue", inputs=inputs, directive=directive)
        return

    if existing.state in ("merged", "closed", "abandoned"):
        if directive is None:
            log.info("skip: comment on finalized issue", extra={"key": key, "state": existing.state})
            try:
                await github.post_comment(
                    repo.full_name,
                    issue.number,
                    persona.finalized_issue_comment(),
                )
            except GitHubError as exc:
                log.warning("ack comment failed", extra={"err": str(exc)})
            return _skipped("skip: comment on finalized issue")
        # Maintainer reopen: tear down stale workspace, reset state, branch
        # afresh from default. The old branch may have been merged/deleted.
        log.info("directive reopen", extra={"key": key, "from_state": existing.state, "author": directive.author})
        await asyncio.to_thread(sandbox.remove_workspace, repo=repo.full_name, number=issue.number)
        db.upsert_issue(key=key, repo=repo.full_name, number=issue.number, state="reproducing")
        workspace = await asyncio.to_thread(
            sandbox.ensure_workspace,
            repo=repo.full_name,
            number=issue.number,
            title=issue.title,
            clone_url=clone_url,
            default_branch=repo.default_branch,
            author_name=settings.resolved_author_name,
            author_email=settings.git_author_email,
            slot_uid=slot_uid,
        )
        db.upsert_issue(
            key=key,
            repo=repo.full_name,
            number=issue.number,
            state="reproducing",
            branch=workspace.branch,
            session_dir=str(workspace.session_dir),
        )
        inputs = TaskInputs(
            settings=settings,
            db=db,
            github=github,
            git_transport=git_transport,
            repo=repo,
            issue=issue,
            workspace=workspace,
            delivery_id=delivery_id,
            attempts=attempts,
            slot_uid=slot_uid,
            natives_cache=sandbox.natives_cache,
        )
        directive = await _attach_thread(github, directive, repo.full_name, issue.number, is_pr=False)
        await run_task(task_kind="handle_comment", inputs=inputs, comment=comment, directive=directive)
        return

    workspace = await asyncio.to_thread(
        sandbox.ensure_workspace,
        repo=repo.full_name,
        number=issue.number,
        title=issue.title,
        clone_url=clone_url,
        default_branch=repo.default_branch,
        existing_branch=existing.branch,
        author_name=settings.resolved_author_name,
        author_email=settings.git_author_email,
        slot_uid=slot_uid,
    )
    inputs = TaskInputs(
        settings=settings,
        db=db,
        github=github,
        git_transport=git_transport,
        repo=repo,
        issue=issue,
        workspace=workspace,
        delivery_id=delivery_id,
        attempts=attempts,
        slot_uid=slot_uid,
        natives_cache=sandbox.natives_cache,
    )
    directive = await _attach_thread(github, directive, repo.full_name, issue.number, is_pr=False)
    await run_task(task_kind="handle_comment", inputs=inputs, comment=comment, directive=directive)


async def handle_review(
    *,
    settings: Settings,
    db: Database,
    github: GitHubBackend,
    sandbox: SandboxManager,
    git_transport: GitTransport,
    payload: Mapping[str, Any],
    delivery_id: str,
    attempts: int = 0,
    slot_uid: int | None = None,
) -> TaskOutcome | None:
    pr = payload.get("pull_request") or {}
    pr_number = int(pr.get("number") or 0)
    if pr_number <= 0:
        log.info("skip: review without PR number")
        return _skipped("skip: review without PR number")
    repo_full = repo_full_name(payload) or ""
    if not repo_full:
        log.info("skip: review without repo")
        return _skipped("skip: review without repo")
    issue_row, pr_info = await _resolve_issue_row_for_pr(
        db=db,
        github=github,
        repo_full=repo_full,
        pr_number=pr_number,
    )
    if issue_row is None:
        if pr_info is None:
            return _skipped("skip: review PR unmapped")
        skip_reason = _direct_pr_skip_reason(settings=settings, repo_full=repo_full, pr=pr_info)
        if skip_reason is not None:
            return _skipped(skip_reason)
        issue_number = pr_number
        existing_branch = pr_info.head_ref
    else:
        if issue_row.branch is None:
            log.info("skip: review PR missing branch mapping", extra={"repo": repo_full, "pr": pr_number})
            return _skipped("skip: review PR missing branch mapping")
        issue_number = issue_row.number
        existing_branch = issue_row.branch
    try:
        repo = await github.get_repo(repo_full)
        issue = await github.get_issue(repo_full, issue_number)
    except GitHubError as exc:
        log.warning("review fetch failed", extra={"err": str(exc)})
        raise _github_fetch_failed(exc) from exc
    clone_url = repo.clone_url
    workspace = sandbox.ensure_workspace(
        repo=repo.full_name,
        number=issue.number,
        title=issue.title,
        clone_url=clone_url,
        default_branch=repo.default_branch,
        existing_branch=existing_branch,
        author_name=settings.resolved_author_name,
        author_email=settings.git_author_email,
        slot_uid=slot_uid,
    )
    if issue_row is None:
        db.upsert_issue(
            key=issue_key(repo_full, pr_number),
            repo=repo_full,
            number=pr_number,
            state="opened",
            branch=workspace.branch,
            session_dir=str(workspace.session_dir),
            pr_number=pr_number,
        )
    comment = payload.get("comment") or {}
    user = comment.get("user") or {}
    review_payload = {
        "author": str(user.get("login") or ""),
        "body": str(comment.get("body") or ""),
        "path": str(comment.get("path") or ""),
        "line": comment.get("line"),
        "start_line": comment.get("start_line"),
        "original_line": comment.get("original_line"),
    }
    inputs = TaskInputs(
        settings=settings,
        db=db,
        github=github,
        git_transport=git_transport,
        repo=repo,
        issue=issue,
        workspace=workspace,
        delivery_id=delivery_id,
        attempts=attempts,
        slot_uid=slot_uid,
        natives_cache=sandbox.natives_cache,
    )
    await run_task(
        task_kind="handle_review",
        inputs=inputs,
        pr_number=pr_number,
        review_payload=review_payload,
    )


async def handle_pr_conversation(
    *,
    settings: Settings,
    db: Database,
    github: GitHubBackend,
    sandbox: SandboxManager,
    git_transport: GitTransport,
    payload: Mapping[str, Any],
    delivery_id: str,
    attempts: int = 0,
    slot_uid: int | None = None,
) -> TaskOutcome | None:
    """Handle a regular (non-review) comment on a bot-authored PR.

    The `issue_comment.created` payload's `issue.number` IS the PR number on
    these events; we resolve back to the originating issue via the DB and
    drive `handle_comment` so the agent works on the same session/branch.
    """
    repo_full = repo_full_name(payload) or ""
    issue_payload = payload.get("issue") or {}
    pr_number = issue_payload.get("number")
    if not repo_full or not isinstance(pr_number, int):
        log.info("skip: pr-conversation missing repo/number")
        return _skipped("skip: pr-conversation missing repo/number")
    issue_row, pr_info = await _resolve_issue_row_for_pr(
        db=db,
        github=github,
        repo_full=repo_full,
        pr_number=pr_number,
    )
    if issue_row is None:
        if pr_info is None:
            return _skipped("skip: pr-conversation PR unmapped")
        skip_reason = _direct_pr_skip_reason(settings=settings, repo_full=repo_full, pr=pr_info)
        if skip_reason is not None:
            return _skipped(skip_reason)
    directive = _directive_from_payload(payload)
    if issue_row is not None and issue_row.state == "reviewing":
        log.info("skip: incoming PR conversation unsupported", extra={"key": issue_row.key, "pr": pr_number})
        return _skipped("skip: incoming PR conversation unsupported")
    if issue_row is not None and issue_row.state in ("merged", "closed", "abandoned"):
        if directive is None:
            log.info("skip: pr-conversation on finalized issue", extra={"key": issue_row.key, "state": issue_row.state})
            # Still acknowledge so the reporter knows the bot saw it.
            try:
                await github.post_comment(
                    repo_full,
                    pr_number,
                    persona.finalized_pr_comment(),
                )
            except GitHubError as exc:
                log.warning("ack comment failed", extra={"err": str(exc)})
            return _skipped("skip: pr-conversation on finalized issue")
        # Maintainer reopen on a finalized PR: tear down stale workspace and
        # branch afresh on the originating issue. The agent will open a new
        # PR if code changes ship.
        log.info(
            "directive reopen (pr)",
            extra={"key": issue_row.key, "from_state": issue_row.state, "author": directive.author},
        )
        await asyncio.to_thread(sandbox.remove_workspace, repo=issue_row.repo, number=issue_row.number)
        db.upsert_issue(key=issue_row.key, repo=issue_row.repo, number=issue_row.number, state="reproducing")
        issue_row = db.get_issue(issue_row.key) or issue_row
    # Bare @mention with no request body — the route stashes an empty
    # _robomp_directive; _directive_from_payload rejects it but the key
    # being present tells us a mention happened. Reply cheaply without omp.
    if directive is None and payload.get("_robomp_directive") is not None:
        comment = _comment_from_payload(payload)
        log.info(
            "bare mention, prompting for request", extra={"repo": repo_full, "pr": pr_number, "author": comment.author}
        )
        try:
            await github.post_comment(repo_full, pr_number, persona.bare_mention_reply())
        except GitHubError as exc:
            log.warning("bare mention reply failed", extra={"err": str(exc)})
        return _skipped("skip: bare mention")
    issue_number = issue_row.number if issue_row is not None else pr_number
    try:
        repo = await github.get_repo(repo_full)
        issue = await github.get_issue(repo_full, issue_number)
    except GitHubError as exc:
        log.warning("pr-conversation fetch failed", extra={"err": str(exc)})
        return _skipped("skip: pr-conversation fetch failed")
    clone_url = repo.clone_url
    if issue_row is None:
        assert pr_info is not None
        existing_branch = pr_info.head_ref
    else:
        # On a reopen the prior branch is stale (merged/deleted), so branch from
        # default; otherwise reuse the existing branch.
        existing_branch = (
            None if directive and issue_row.state == "reproducing" and issue_row.branch is None else issue_row.branch
        )
        if existing_branch is None and not (directive and issue_row.state == "reproducing"):
            log.info("skip: pr-conversation PR missing branch mapping", extra={"repo": repo_full, "pr": pr_number})
            return _skipped("skip: pr-conversation PR missing branch mapping")
    workspace = sandbox.ensure_workspace(
        repo=repo.full_name,
        number=issue.number,
        title=issue.title,
        clone_url=clone_url,
        default_branch=repo.default_branch,
        existing_branch=existing_branch,
        author_name=settings.resolved_author_name,
        author_email=settings.git_author_email,
        slot_uid=slot_uid,
    )
    if issue_row is None:
        db.upsert_issue(
            key=issue_key(repo_full, pr_number),
            repo=repo_full,
            number=pr_number,
            state="opened",
            branch=workspace.branch,
            session_dir=str(workspace.session_dir),
            pr_number=pr_number,
        )
    elif directive is not None and (issue_row.branch is None or issue_row.branch != workspace.branch):
        db.upsert_issue(
            key=issue_row.key,
            repo=issue_row.repo,
            number=issue_row.number,
            state="reproducing",
            branch=workspace.branch,
            session_dir=str(workspace.session_dir),
        )
    comment = _comment_from_payload(payload)
    inputs = TaskInputs(
        settings=settings,
        db=db,
        github=github,
        git_transport=git_transport,
        repo=repo,
        issue=issue,
        workspace=workspace,
        delivery_id=delivery_id,
        attempts=attempts,
        slot_uid=slot_uid,
        natives_cache=sandbox.natives_cache,
    )
    thread: tuple[ThreadMessage, ...] = ()
    if directive is None:
        thread = await _fetch_thread(github, repo_full, pr_number, is_pr=True)
    else:
        directive = await _attach_thread(github, directive, repo_full, pr_number, is_pr=True)
    await run_task(
        task_kind="handle_comment",
        inputs=inputs,
        comment=comment,
        pr_number=pr_number,
        directive=directive,
        thread=thread,
    )


async def cleanup_workspace(
    *,
    settings: Settings,
    db: Database,
    sandbox: SandboxManager,
    payload: Mapping[str, Any],
    target_state: IssueState,
) -> TaskOutcome | None:
    """Tear down the workspace for a finished issue/PR."""
    repo_full = repo_full_name(payload) or ""
    if not repo_full:
        return _skipped("skip: cleanup missing repo")
    issue_payload = payload.get("issue") or payload.get("pull_request") or {}
    number = issue_payload.get("number")
    if not isinstance(number, int):
        return _skipped("skip: cleanup missing issue number")
    # If this is a PR close, map to the originating issue first, then fall back
    # to a directly-keyed issue row.
    is_pr = "pull_request" in payload
    if is_pr:
        issue_row = db.find_issue_by_pr(repo_full, number) or db.get_issue(issue_key(repo_full, number))
    else:
        issue_row = db.get_issue(issue_key(repo_full, number))
    if issue_row is not None:
        await asyncio.to_thread(sandbox.remove_workspace, repo=issue_row.repo, number=issue_row.number)
        db.set_issue_state(issue_row.key, target_state)
        log.info("cleanup", extra={"key": issue_row.key, "state": target_state})
        return None
    if is_pr:
        # Direct incoming-PR workspace never mapped to an originating issue.
        await asyncio.to_thread(sandbox.remove_workspace, repo=repo_full, number=number)
        log.info("cleanup direct pr workspace", extra={"repo": repo_full, "pr": number, "state": target_state})
        return None
    return _skipped("skip: cleanup missing issue row")


__all__ = [
    "cleanup_workspace",
    "handle_comment",
    "handle_pr_conversation",
    "handle_review",
    "review_pr",
    "triage_issue",
]
