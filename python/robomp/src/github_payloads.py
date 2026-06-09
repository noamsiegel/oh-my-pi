"""Webhook payload projections used by routing and task startup."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from robomp.github_types import IssueInfo, IssueSummary, RepoInfo


def repo_full_name(payload: Mapping[str, Any]) -> str | None:
    repo = payload.get("repository")
    if isinstance(repo, Mapping):
        full_name = repo.get("full_name")
        if isinstance(full_name, str) and full_name:
            return full_name
    return None


def label_names(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(label.get("name") or "") if isinstance(label, Mapping) else str(label) for label in raw)


def repo_from_payload(data: Mapping[str, Any]) -> RepoInfo:
    return RepoInfo(
        full_name=str(data["full_name"]),
        default_branch=str(data["default_branch"]),
        clone_url=str(data["clone_url"]),
        private=bool(data.get("private", False)),
    )


def issue_from_payload(repo: str, data: Mapping[str, Any]) -> IssueInfo:
    user = data.get("user") or {}
    return IssueInfo(
        repo=repo,
        number=int(data["number"]),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        state=str(data.get("state") or "open"),
        author=str(user.get("login") or "") if isinstance(user, Mapping) else "",
        labels=label_names(data.get("labels")),
        is_pull_request="pull_request" in data,
    )


def issue_summary_from_payload(repo: str, issue: Mapping[str, Any]) -> IssueSummary | None:
    number = issue.get("number")
    if not isinstance(number, int):
        return None
    user = issue.get("user")
    state = str(issue.get("state") or "open").lower()
    if state not in {"open", "closed"}:
        state = "open"
    comments = issue.get("comments")
    if not isinstance(comments, int):
        comments = 0
    return IssueSummary(
        repo=repo,
        number=number,
        title=str(issue.get("title") or ""),
        state=state,
        author=str(user.get("login") or "") if isinstance(user, Mapping) else "",
        labels=label_names(issue.get("labels")),
        comments=comments,
        updated_at=str(issue.get("updated_at") or issue.get("created_at") or ""),
        created_at=str(issue.get("created_at") or ""),
        html_url=str(issue.get("html_url") or f"https://github.com/{repo}/issues/{number}"),
    )


def parse_issue_payload(payload: Mapping[str, Any]) -> tuple[RepoInfo, IssueInfo]:
    """Build typed records from a webhook payload (issues.opened, etc.)."""
    repo_payload = payload["repository"]
    if not isinstance(repo_payload, Mapping):
        raise TypeError("repository must be an object")
    repo = repo_from_payload(repo_payload)
    issue_payload = payload["issue"]
    if not isinstance(issue_payload, Mapping):
        raise TypeError("issue must be an object")
    issue = issue_from_payload(repo.full_name, issue_payload)
    return repo, issue


__all__ = [
    "issue_from_payload",
    "issue_summary_from_payload",
    "label_names",
    "parse_issue_payload",
    "repo_from_payload",
    "repo_full_name",
]
