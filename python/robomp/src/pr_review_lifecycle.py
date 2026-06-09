"""Advisory PR-review lifecycle observer and learning signals."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, Literal

from robomp.db import Database, PrReviewPostedFinding

log = logging.getLogger(__name__)

SourceLabel = Literal["human", "copilot", "agent", "unknown"]
SeverityHint = Literal["critical", "required", "optional", "unknown"]

_AGENT_RE = re.compile(r"claude|codex|openai|anthropic|chatgpt|cursor|agent", re.I)
_CRITICAL_RE = re.compile(r"security|data loss|critical|blocker|must fix|mustfix", re.I)
_REQUIRED_RE = re.compile(r"should|bug|incorrect|break|regression|required|important", re.I)
_OPTIONAL_RE = re.compile(r"nit|optional|consider|style|typo", re.I)
_DISAGREE_RE = re.compile(r"disagree|not a bug|intended|won't fix|works as designed|false positive", re.I)
_SUGGESTION_RE = re.compile(r"```suggestion\n(.*?)```", re.I | re.S)


def body_hash(body: str) -> str:
    return sha256(body.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def source_label(login: str, user_type: str | None, body: str) -> SourceLabel:
    text = f"{login}\n{body}"
    if "copilot" in text.lower():
        return "copilot"
    if user_type == "Bot" or _AGENT_RE.search(text):
        return "agent"
    if login:
        return "human"
    return "unknown"


def severity_hint(body: str) -> SeverityHint:
    if _CRITICAL_RE.search(body):
        return "critical"
    if _REQUIRED_RE.search(body):
        return "required"
    if _OPTIONAL_RE.search(body):
        return "optional"
    return "unknown"


def extract_github_suggestion_hash(body: str) -> str | None:
    match = _SUGGESTION_RE.search(body)
    if match is None:
        return None
    replacement = match.group(1).replace("\r\n", "\n")
    return sha256(replacement.encode("utf-8")).hexdigest()


def _repo(payload: Mapping[str, Any]) -> str | None:
    repo = payload.get("repository")
    if isinstance(repo, Mapping):
        full_name = repo.get("full_name")
        if isinstance(full_name, str) and full_name:
            return full_name
    return None


def _user(obj: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    if not isinstance(obj, Mapping):
        return None, None
    user = obj.get("user")
    if not isinstance(user, Mapping):
        return None, None
    login = user.get("login")
    user_type = user.get("type")
    return (str(login) if isinstance(login, str) else None, str(user_type) if isinstance(user_type, str) else None)


def _association(obj: Mapping[str, Any] | None) -> str | None:
    if not isinstance(obj, Mapping):
        return None
    assoc = obj.get("author_association")
    return str(assoc) if isinstance(assoc, str) and assoc else None


def _str_id(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _pr_head_sha(payload: Mapping[str, Any]) -> str | None:
    pr = payload.get("pull_request")
    if isinstance(pr, Mapping):
        head = pr.get("head")
        if isinstance(head, Mapping) and isinstance(head.get("sha"), str):
            return str(head["sha"])
    return None


def _pr_number_from_payload(payload: Mapping[str, Any]) -> int | None:
    pr = payload.get("pull_request")
    if isinstance(pr, Mapping) and isinstance(pr.get("number"), int):
        return int(pr["number"])
    issue = payload.get("issue")
    if isinstance(issue, Mapping) and "pull_request" in issue and isinstance(issue.get("number"), int):
        return int(issue["number"])
    return None


def lifecycle_fact_from_payload(event_type: str, payload: Mapping[str, Any], *, delivery_id: str) -> dict[str, Any] | None:
    action = str(payload.get("action") or "")
    repo = _repo(payload)
    if repo is None:
        return None
    base: dict[str, Any] = {"delivery_id": delivery_id, "event_type": event_type, "action": action, "repo": repo}

    if event_type == "pull_request" and action in {"opened", "reopened", "ready_for_review", "labeled", "synchronize", "closed"}:
        pr = payload.get("pull_request")
        if not isinstance(pr, Mapping) or not isinstance(pr.get("number"), int):
            return None
        login, user_type = _user(pr)
        return {
            **base,
            "pr_number": int(pr["number"]),
            "head_sha": _pr_head_sha(payload),
            "actor_login": login,
            "actor_type": user_type,
            "author_association": _association(pr),
            "object_kind": "pull_request",
            "object_id": str(pr["number"]),
            "merged": bool(pr.get("merged")) if action == "closed" else None,
            "created_at": str(pr.get("updated_at") or pr.get("created_at") or "") or None,
        }

    if event_type == "pull_request_review" and action in {"submitted", "dismissed", "edited"}:
        review = payload.get("review")
        pr_number = _pr_number_from_payload(payload)
        if not isinstance(review, Mapping) or pr_number is None:
            return None
        login, user_type = _user(review)
        return {
            **base,
            "pr_number": pr_number,
            "head_sha": str(review.get("commit_id") or _pr_head_sha(payload) or "") or None,
            "actor_login": login,
            "actor_type": user_type,
            "author_association": _association(review),
            "object_kind": "review",
            "object_id": _str_id(review.get("id")),
            "review_state": str(review.get("state") or "") or None,
            "body": str(review.get("body") or ""),
            "created_at": str(review.get("submitted_at") or review.get("updated_at") or "") or None,
        }

    if event_type == "pull_request_review_comment" and action in {"created", "edited", "deleted"}:
        comment = payload.get("comment")
        pr_number = _pr_number_from_payload(payload)
        if not isinstance(comment, Mapping) or pr_number is None:
            return None
        login, user_type = _user(comment)
        return {
            **base,
            "pr_number": pr_number,
            "head_sha": str(comment.get("commit_id") or _pr_head_sha(payload) or "") or None,
            "actor_login": login,
            "actor_type": user_type,
            "author_association": _association(comment),
            "object_kind": "review_comment",
            "object_id": _str_id(comment.get("id")),
            "parent_id": _str_id(comment.get("in_reply_to_id")),
            "path": str(comment.get("path") or "") or None,
            "line": int(comment["line"]) if isinstance(comment.get("line"), int) else None,
            "start_line": int(comment["start_line"]) if isinstance(comment.get("start_line"), int) else None,
            "body": str(comment.get("body") or ""),
            "suggestion_hash": extract_github_suggestion_hash(str(comment.get("body") or "")),
            "thread_id": _str_id(comment.get("pull_request_review_thread_id") or comment.get("thread_id")),
            "created_at": str(comment.get("created_at") or comment.get("updated_at") or "") or None,
        }

    if event_type == "pull_request_review_thread" and action in {"resolved", "unresolved"}:
        thread = payload.get("thread")
        pr_number = _pr_number_from_payload(payload)
        if not isinstance(thread, Mapping) or pr_number is None:
            return None
        return {
            **base,
            "pr_number": pr_number,
            "head_sha": _pr_head_sha(payload),
            "object_kind": "review_thread",
            "object_id": _str_id(thread.get("id")),
            "thread_id": _str_id(thread.get("id")),
            "thread_resolved": action == "resolved",
            "created_at": str(thread.get("updated_at") or thread.get("created_at") or "") or None,
        }

    if event_type == "issue_comment" and action == "created":
        issue = payload.get("issue")
        comment = payload.get("comment")
        if not isinstance(issue, Mapping) or "pull_request" not in issue or not isinstance(comment, Mapping):
            return None
        number = issue.get("number")
        if not isinstance(number, int):
            return None
        login, user_type = _user(comment)
        return {
            **base,
            "pr_number": int(number),
            "head_sha": _pr_head_sha(payload),
            "actor_login": login,
            "actor_type": user_type,
            "author_association": _association(comment),
            "object_kind": "conversation_comment",
            "object_id": _str_id(comment.get("id")),
            "body": str(comment.get("body") or ""),
            "suggestion_hash": extract_github_suggestion_hash(str(comment.get("body") or "")),
            "created_at": str(comment.get("created_at") or comment.get("updated_at") or "") or None,
        }

    return None


def _latest_review_findings(findings: list[PrReviewPostedFinding]) -> list[PrReviewPostedFinding]:
    review_ids = [f.review_id for f in findings if f.review_id is not None]
    if not review_ids:
        return findings
    latest = max(review_ids)
    return [f for f in findings if f.review_id == latest]


def _matching_posted_finding(fact: Mapping[str, Any], findings: list[PrReviewPostedFinding]) -> PrReviewPostedFinding | None:
    body = str(fact.get("body") or "")
    bh = body_hash(body) if body else None
    path = fact.get("path")
    line = fact.get("line")
    for finding in findings:
        if bh is not None and finding.body_hash == bh:
            return finding
        if path and finding.path == path and line is not None and finding.line is not None and abs(int(line) - int(finding.line)) <= 5:
            return finding
    return None


def _thread_comment_ids(payload: Mapping[str, Any]) -> list[int]:
    thread = payload.get("thread")
    if not isinstance(thread, Mapping):
        return []
    ids: list[int] = []
    comments = thread.get("comments")
    if isinstance(comments, list):
        for comment in comments:
            if isinstance(comment, Mapping) and isinstance(comment.get("id"), int):
                ids.append(int(comment["id"]))
    return ids


def _observe_webhook_inner(db: Database, *, event_type: str, delivery_id: str, payload: Mapping[str, Any], bot_login: str, allowlist: frozenset[str]) -> None:
    fact = lifecycle_fact_from_payload(event_type, payload, delivery_id=delivery_id)
    if fact is None:
        return
    repo = str(fact["repo"])
    if repo.lower() not in allowlist:
        return
    pr_number = int(fact["pr_number"])
    db.record_pr_review_lifecycle_event(**fact)

    actor_login = str(fact.get("actor_login") or "")
    if actor_login == bot_login:
        return

    if event_type == "pull_request_review_thread" and fact.get("thread_resolved") is True:
        for comment_id in _thread_comment_ids(payload):
            db.update_pr_review_posted_finding_status(comment_id=comment_id, status="resolved", reason="review thread resolved")
        return

    if event_type == "pull_request" and fact.get("action") == "synchronize":
        head_sha = fact.get("head_sha")
        for finding in db.list_pr_review_posted_findings(repo, pr_number):
            if finding.head_sha and finding.head_sha != head_sha:
                db.update_pr_review_posted_finding_status(finding_id=finding.finding_id, status="obsolete", reason="new head sha")
        return

    if event_type == "pull_request" and fact.get("action") == "closed" and fact.get("merged") is True:
        for finding in db.list_pr_review_posted_findings(repo, pr_number):
            if finding.severity in {"critical", "required"} and finding.status in {"posted", "unaddressed", "suggestion_ignored"}:
                db.record_pr_review_gap_event(
                    gap_kind="merged_with_unaddressed_required",
                    repo=repo,
                    pr_number=pr_number,
                    head_sha=finding.head_sha,
                    source_label="agent",
                    source_object_kind="pull_request",
                    source_object_id=str(pr_number),
                    agent_review_id=finding.review_id,
                    agent_comment_id=finding.comment_id,
                    matched_posted_finding_id=finding.finding_id,
                    path=finding.path,
                    line=finding.line,
                    start_line=finding.start_line,
                    body=finding.body,
                    severity_hint=finding.severity,
                    confidence=1.0,
                    reason="PR merged with required agent finding still unaddressed",
                )
        return

    body = str(fact.get("body") or "")
    label = source_label(actor_login, fact.get("actor_type"), body)
    if label != "human":
        return
    object_kind = str(fact.get("object_kind") or "")
    is_review_comment = event_type == "pull_request_review_comment" and fact.get("action") == "created"
    is_review_body = event_type == "pull_request_review" and fact.get("action") == "submitted" and bool(body.strip())
    is_issue_comment = event_type == "issue_comment" and fact.get("action") == "created"
    if not (is_review_comment or is_review_body or is_issue_comment):
        return

    findings = db.list_pr_review_posted_findings(repo, pr_number)
    if not findings:
        return
    latest = _latest_review_findings(findings)

    parent_id = fact.get("parent_id")
    if parent_id is not None:
        for finding in latest:
            if finding.comment_id is not None and str(finding.comment_id) == str(parent_id) and _DISAGREE_RE.search(body):
                db.update_pr_review_posted_finding_status(comment_id=finding.comment_id, status="false_positive", reason="human disagreement")
                db.record_pr_review_gap_event(
                    gap_kind="human_disagreement",
                    repo=repo,
                    pr_number=pr_number,
                    head_sha=fact.get("head_sha"),
                    source_label=label,
                    actor_login=actor_login,
                    event_delivery_id=delivery_id,
                    source_object_kind=object_kind,
                    source_object_id=fact.get("object_id"),
                    agent_review_id=finding.review_id,
                    agent_comment_id=finding.comment_id,
                    matched_posted_finding_id=finding.finding_id,
                    path=finding.path,
                    line=finding.line,
                    start_line=finding.start_line,
                    body=body,
                    severity_hint=severity_hint(body),
                    confidence=0.9,
                    reason="human reply disagreed with posted agent finding",
                    created_at=fact.get("created_at"),
                )
                return

    matched = _matching_posted_finding(fact, latest)
    if matched is not None:
        db.record_pr_review_gap_event(
            gap_kind="duplicate_of_agent",
            repo=repo,
            pr_number=pr_number,
            head_sha=fact.get("head_sha"),
            source_label=label,
            actor_login=actor_login,
            event_delivery_id=delivery_id,
            source_object_kind=object_kind,
            source_object_id=fact.get("object_id"),
            agent_review_id=matched.review_id,
            agent_comment_id=matched.comment_id,
            matched_posted_finding_id=matched.finding_id,
            path=fact.get("path") or matched.path,
            line=fact.get("line") or matched.line,
            start_line=fact.get("start_line") or matched.start_line,
            body=body,
            severity_hint=severity_hint(body),
            confidence=0.9,
            reason="human review signal matched posted agent finding",
            created_at=fact.get("created_at"),
        )
        return

    db.record_pr_review_gap_event(
        gap_kind="missed_by_agent",
        repo=repo,
        pr_number=pr_number,
        head_sha=fact.get("head_sha"),
        source_label=label,
        actor_login=actor_login,
        event_delivery_id=delivery_id,
        source_object_kind=object_kind,
        source_object_id=fact.get("object_id"),
        path=fact.get("path"),
        line=fact.get("line"),
        start_line=fact.get("start_line"),
        body=body,
        severity_hint=severity_hint(body),
        confidence=0.8,
        reason="human review signal after latest agent review did not match any posted agent finding",
        created_at=fact.get("created_at"),
    )


def observe_webhook(db: Database, *, event_type: str, delivery_id: str, payload: Mapping[str, Any], bot_login: str, allowlist: frozenset[str]) -> None:
    try:
        _observe_webhook_inner(db, event_type=event_type, delivery_id=delivery_id, payload=payload, bot_login=bot_login, allowlist=allowlist)
    except Exception:
        log.exception("PR review lifecycle observation failed")
