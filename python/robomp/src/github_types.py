"""Typed GitHub value objects shared by direct and proxy clients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


class GitHubError(RuntimeError):
    """Raised on non-2xx responses from GitHub."""

    def __init__(self, status: int, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(f"GitHub {status}: {message}")
        self.status = status
        self.message = message
        self.retry_after = retry_after


@dataclass(slots=True, frozen=True)
class IssueInfo:
    repo: str
    number: int
    title: str
    body: str
    state: str
    author: str
    labels: tuple[str, ...]
    is_pull_request: bool


@dataclass(slots=True, frozen=True)
class CommentInfo:
    id: int
    author: str
    body: str
    created_at: str


@dataclass(slots=True, frozen=True)
class RepoInfo:
    full_name: str
    default_branch: str
    clone_url: str
    private: bool


@dataclass(slots=True, frozen=True)
class PullRequestInfo:
    repo: str
    number: int
    html_url: str
    head_ref: str
    base_ref: str
    state: str
    author: str = ""
    head_repo: str = ""
    title: str = ""
    body: str = ""
    draft: bool = False
    head_sha: str = ""
    author_type: str = ""
    labels: tuple[str, ...] = ()
    updated_at: str = ""
    merged: bool = False


@dataclass(slots=True, frozen=True)
class PullRequestFileInfo:
    path: str
    status: str
    additions: int
    deletions: int
    previous_filename: str = ""


@dataclass(slots=True, frozen=True)
class PullRequestCommitAuthorInfo:
    login: str = ""
    name: str = ""


@dataclass(slots=True, frozen=True)
class PullRequestCommitInfo:
    sha: str
    message_headline: str
    message_body: str
    authors: tuple[PullRequestCommitAuthorInfo, ...] = ()

CiCheckState = Literal["pending", "passed", "failed"]


@dataclass(slots=True, frozen=True)
class PullRequestCiCheckInfo:
    name: str
    state: CiCheckState
    source: str  # "check_run" or "status"
    status: str = ""
    conclusion: str = ""
    details_url: str = ""


@dataclass(slots=True, frozen=True)
class PullRequestCiStatusInfo:
    head_sha: str
    state: CiCheckState
    total_count: int
    pending_count: int
    failed_count: int
    checks: tuple[PullRequestCiCheckInfo, ...] = ()


@dataclass(slots=True, frozen=True)
class ReviewCommentInfo:
    """In-line PR review comment (attached to a file/line)."""

    id: int
    author: str
    body: str
    path: str
    line: int | None
    created_at: str
    start_line: int | None = None
    original_line: int | None = None
    html_url: str = ""
    review_id: int | None = None
    commit_id: str = ""
    diff_hunk: str = ""
    in_reply_to_id: int | None = None


@dataclass(slots=True, frozen=True)
class ReviewThreadCommentInfo:
    """Inline PR review comment with GraphQL thread state attached."""

    id: int
    author: str
    body: str
    path: str
    line: int | None
    created_at: str
    start_line: int | None = None
    original_line: int | None = None
    original_start_line: int | None = None
    html_url: str = ""
    review_id: int | None = None
    commit_id: str = ""
    diff_hunk: str = ""
    in_reply_to_id: int | None = None
    is_outdated: bool = False
    state: str = ""


@dataclass(slots=True, frozen=True)
class ReviewThreadInfo:
    """GraphQL PR review thread, including resolution/outdated state."""

    id: str
    is_resolved: bool
    is_outdated: bool
    resolved_by: str = ""
    comments: tuple[ReviewThreadCommentInfo, ...] = ()


@dataclass(slots=True, frozen=True)
class PullRequestReviewInfo:
    """Top-level PR review (the summary block, not the inline comments)."""

    id: int
    author: str
    body: str
    state: str  # APPROVED / CHANGES_REQUESTED / COMMENTED
    submitted_at: str
    commit_id: str = ""


@dataclass(slots=True, frozen=True)
class IssueSummary:
    """Lightweight projection of an issue for list views (no body)."""

    repo: str
    number: int
    title: str
    state: str
    author: str
    labels: tuple[str, ...]
    comments: int
    updated_at: str
    created_at: str
    html_url: str


@dataclass(slots=True, frozen=True)
class ReactionInfo:
    """A reaction on an issue/comment.

    `content` is GitHub's reaction string: `+1`, `-1`, `laugh`, `hooray`,
    `confused`, `heart`, `rocket`, `eyes`. The auto-close scheduler only
    looks at `-1` (👎) reactions from the issue's original author.
    """

    content: str
    user_login: str
    user_type: str


__all__ = [
    "CiCheckState",
    "CommentInfo",
    "GitHubError",
    "IssueInfo",
    "IssueSummary",
    "PullRequestCiCheckInfo",
    "PullRequestCiStatusInfo",
    "PullRequestCommitAuthorInfo",
    "PullRequestCommitInfo",
    "PullRequestFileInfo",
    "PullRequestInfo",
    "PullRequestReviewInfo",
    "ReactionInfo",
    "RepoInfo",
    "ReviewCommentInfo",
    "ReviewThreadCommentInfo",
    "ReviewThreadInfo",
]
