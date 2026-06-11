from __future__ import annotations

from robomp.db import PrReviewPostedFinding
from robomp.github_types import PullRequestReviewInfo
from robomp.pr_review_suggestion_fastpath import (
    accepted_suggestions_fast_path_result,
    extract_github_suggestion_replacement,
)

OLD_SHA = "old-sha"
HEAD_SHA = "head-sha"


def _review() -> PullRequestReviewInfo:
    return PullRequestReviewInfo(
        id=123,
        author="robomp-bot",
        body="fix this",
        state="CHANGES_REQUESTED",
        submitted_at="2026-06-10T00:00:00Z",
        commit_id=OLD_SHA,
    )


def _finding(*, replacement: str | None = "return value;", path: str = "src/example.ts") -> PrReviewPostedFinding:
    return PrReviewPostedFinding(
        finding_id=f"finding-{path}-{replacement}",
        issue_key="octo/widget#1",
        repo="octo/widget",
        pr_number=1,
        head_sha=OLD_SHA,
        review_id=123,
        comment_id=456,
        path=path,
        line=10,
        start_line=10,
        body="Please apply this.",
        body_hash="body-hash",
        severity="required",
        intent="required_change",
        category=None,
        suggestion_replacement=replacement,
        suggestion_hash=None,
        status="posted",
        status_reason=None,
        posted_at="2026-06-10T00:00:00Z",
        updated_at="2026-06-10T00:00:00Z",
    )


def _delta(added: str = "return value;") -> str:
    return f"""diff --git a/src/example.ts b/src/example.ts
--- a/src/example.ts
+++ b/src/example.ts
@@ -10,1 +10,1 @@
-return old_value;
+{added}
"""


def test_extract_github_suggestion_replacement_normalizes_newline() -> None:
    body = "Please patch.\r\n```suggestion\r\nreturn value;\r\n```"
    assert extract_github_suggestion_replacement(body) == "return value;"


def test_fast_path_accepts_exact_single_suggestion_delta() -> None:
    result = accepted_suggestions_fast_path_result(
        latest_review=_review(),
        current_head_sha=HEAD_SHA,
        posted_findings=[_finding()],
        delta_diff=_delta(),
        terminal_events_enabled=True,
        bot_login="robomp-bot",
        pr_author="alice",
    )
    assert result.eligible is True
    assert result.event == "APPROVE"
    assert len(result.suggestions) == 1


def test_fast_path_rejects_modified_suggestion() -> None:
    result = accepted_suggestions_fast_path_result(
        latest_review=_review(),
        current_head_sha=HEAD_SHA,
        posted_findings=[_finding()],
        delta_diff=_delta("return modified_value;"),
        terminal_events_enabled=True,
        bot_login="robomp-bot",
        pr_author="alice",
    )
    assert result.eligible is False
    assert result.reason == "suggestion_not_applied_exactly"


def test_fast_path_rejects_extra_delta_hunk() -> None:
    extra_delta = _delta() + """diff --git a/src/other.ts b/src/other.ts
--- a/src/other.ts
+++ b/src/other.ts
@@ -3,1 +3,1 @@
-old
+new
"""
    result = accepted_suggestions_fast_path_result(
        latest_review=_review(),
        current_head_sha=HEAD_SHA,
        posted_findings=[_finding()],
        delta_diff=extra_delta,
        terminal_events_enabled=True,
        bot_login="robomp-bot",
        pr_author="alice",
    )
    assert result.eligible is False
    assert result.reason == "delta_contains_non_suggestion_changes"


def test_fast_path_rejects_non_suggestion_blocker() -> None:
    result = accepted_suggestions_fast_path_result(
        latest_review=_review(),
        current_head_sha=HEAD_SHA,
        posted_findings=[_finding(replacement=None)],
        delta_diff=_delta(),
        terminal_events_enabled=True,
        bot_login="robomp-bot",
        pr_author="alice",
    )
    assert result.eligible is False
    assert result.reason == "prior_review_has_non_suggestion_blocker"


def test_fast_path_comments_when_terminal_events_disabled() -> None:
    result = accepted_suggestions_fast_path_result(
        latest_review=_review(),
        current_head_sha=HEAD_SHA,
        posted_findings=[_finding()],
        delta_diff=_delta(),
        terminal_events_enabled=False,
        bot_login="robomp-bot",
        pr_author="alice",
    )
    assert result.eligible is True
    assert result.event == "COMMENT"
