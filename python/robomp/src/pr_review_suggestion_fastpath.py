from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from robomp.db import PrReviewPostedFinding
from robomp.github_types import PullRequestReviewInfo

SuggestionFastPathEvent = Literal["APPROVE", "COMMENT"]


@dataclass(slots=True, frozen=True)
class PriorSuggestion:
    finding_id: str
    comment_id: int | None
    review_id: int | None
    severity: str
    path: str
    line: int
    start_line: int
    replacement: str
    replacement_hash: str


@dataclass(slots=True, frozen=True)
class DiffHunk:
    path: str
    old_start: int
    old_end: int
    added_text: str


@dataclass(slots=True, frozen=True)
class AcceptedSuggestionFastPathResult:
    eligible: bool
    event: SuggestionFastPathEvent | None
    reason: str
    suggestions: tuple[PriorSuggestion, ...] = ()
    extra_delta_paths: tuple[str, ...] = ()


def extract_github_suggestion_replacement(body: str) -> str | None:
    match = re.search(r"```suggestion\s*\n([\s\S]*?)```", body, re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).replace("\r\n", "\n").removesuffix("\n")


def suggestion_hash(replacement: str) -> str:
    return hashlib.sha256(replacement.replace("\r\n", "\n").encode()).hexdigest()


def _strip_diff_path_prefix(path: str) -> str:
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _parse_diff_path(line: str) -> str | None:
    path = line[4:].strip().split("\t", 1)[0]
    if not path or path == "/dev/null":
        return None
    return _strip_diff_path_prefix(path)


def parse_unified_diff_hunks_by_path(delta_diff: str | None) -> dict[str, tuple[DiffHunk, ...]]:
    diff = (delta_diff or "").replace("\r\n", "\n")
    by_path: dict[str, list[DiffHunk]] = {}
    current_path: str | None = None
    current_old_start: int | None = None
    current_old_end: int | None = None
    current_added_lines: list[str] = []

    def flush_hunk() -> None:
        nonlocal current_old_start, current_old_end, current_added_lines
        if current_path is not None and current_old_start is not None and current_old_end is not None:
            by_path.setdefault(current_path, []).append(
                DiffHunk(
                    path=current_path,
                    old_start=current_old_start,
                    old_end=current_old_end,
                    added_text="\n".join(current_added_lines),
                )
            )
        current_old_start = None
        current_old_end = None
        current_added_lines = []

    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            flush_hunk()
            current_path = None
            match = re.match(r"^diff --git\s+a/(.+?)\s+b/(.+)$", line)
            if match is not None:
                current_path = match.group(2)
            continue
        if line.startswith("+++ "):
            path = _parse_diff_path(line)
            if path is not None:
                current_path = path
            continue
        if line.startswith("@@ "):
            flush_hunk()
            match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", line)
            if match is None or current_path is None:
                continue
            old_start = int(match.group(1))
            old_len = int(match.group(2) or "1")
            current_old_start = old_start
            current_old_end = old_start if old_len == 0 else old_start + old_len - 1
            current_added_lines = []
            continue
        if current_old_start is not None and line.startswith("+") and not line.startswith("+++"):
            current_added_lines.append(line[1:])
    flush_hunk()
    return {path: tuple(hunks) for path, hunks in by_path.items()}


def hunk_overlaps_line_range(hunk: DiffHunk, start: int, end: int) -> bool:
    return hunk.old_start <= end and start <= hunk.old_end

def _added_lines_include_exact_replacement(added_text: str, replacement: str) -> bool:
    if not replacement:
        return False
    added_lines = added_text.split("\n")
    replacement_lines = replacement.split("\n")
    window = len(replacement_lines)
    for idx in range(0, len(added_lines) - window + 1):
        if added_lines[idx : idx + window] == replacement_lines:
            return True
    return False



def _same_review(finding: PrReviewPostedFinding, prior_review_id: int) -> bool:
    return str(finding.review_id) == str(prior_review_id)


def _is_blocking(finding: PrReviewPostedFinding) -> bool:
    return finding.severity in {"critical", "required"}


def prior_review_suggestions(
    *,
    posted_findings: Sequence[PrReviewPostedFinding],
    prior_review_id: int,
) -> tuple[PriorSuggestion, ...]:
    suggestions: list[PriorSuggestion] = []
    for finding in posted_findings:
        if not _same_review(finding, prior_review_id):
            continue
        if not _is_blocking(finding):
            continue
        if not finding.path:
            continue
        if not isinstance(finding.line, int):
            continue
        if not finding.suggestion_replacement:
            continue
        replacement = finding.suggestion_replacement
        suggestions.append(
            PriorSuggestion(
                finding_id=finding.finding_id,
                comment_id=finding.comment_id,
                review_id=finding.review_id,
                severity=finding.severity,
                path=finding.path,
                line=finding.line,
                start_line=finding.start_line or finding.line,
                replacement=replacement,
                replacement_hash=finding.suggestion_hash or suggestion_hash(replacement),
            )
        )
    return tuple(suggestions)


def accepted_suggestions_fast_path_result(
    *,
    latest_review: PullRequestReviewInfo | None,
    current_head_sha: str,
    posted_findings: Sequence[PrReviewPostedFinding],
    delta_diff: str | None,
    terminal_events_enabled: bool,
    bot_login: str,
    pr_author: str,
) -> AcceptedSuggestionFastPathResult:
    if latest_review is None:
        return AcceptedSuggestionFastPathResult(False, None, "no_latest_review")
    if latest_review.state.upper() != "CHANGES_REQUESTED":
        return AcceptedSuggestionFastPathResult(False, None, "latest_review_not_changes_requested")
    if not latest_review.commit_id:
        return AcceptedSuggestionFastPathResult(False, None, "latest_review_missing_commit")
    if latest_review.commit_id == current_head_sha:
        return AcceptedSuggestionFastPathResult(False, None, "latest_review_same_head")

    blocking_findings = tuple(
        finding for finding in posted_findings if _same_review(finding, latest_review.id) and _is_blocking(finding)
    )
    if not blocking_findings:
        return AcceptedSuggestionFastPathResult(False, None, "no_recorded_blocking_suggestions")
    if any(not finding.suggestion_replacement for finding in blocking_findings):
        return AcceptedSuggestionFastPathResult(False, None, "prior_review_has_non_suggestion_blocker")

    suggestions = prior_review_suggestions(posted_findings=posted_findings, prior_review_id=latest_review.id)
    if len(suggestions) != len(blocking_findings):
        return AcceptedSuggestionFastPathResult(False, None, "prior_review_has_non_suggestion_blocker")
    if not delta_diff or not delta_diff.strip():
        return AcceptedSuggestionFastPathResult(False, None, "delta_diff_unavailable", suggestions=suggestions)

    hunks_by_path = parse_unified_diff_hunks_by_path(delta_diff)
    hunks = tuple(hunk for path_hunks in hunks_by_path.values() for hunk in path_hunks)
    if not hunks:
        return AcceptedSuggestionFastPathResult(False, None, "delta_diff_unparseable", suggestions=suggestions)

    extra_paths: set[str] = set()
    for hunk in hunks:
        if not any(
            suggestion.path == hunk.path
            and hunk_overlaps_line_range(hunk, suggestion.start_line, suggestion.line)
            for suggestion in suggestions
        ):
            extra_paths.add(hunk.path)
    if extra_paths:
        return AcceptedSuggestionFastPathResult(
            False,
            None,
            "delta_contains_non_suggestion_changes",
            suggestions=suggestions,
            extra_delta_paths=tuple(sorted(extra_paths)),
        )

    for suggestion in suggestions:
        matching_hunks = (
            hunk
            for hunk in hunks_by_path.get(suggestion.path, ())
            if hunk_overlaps_line_range(hunk, suggestion.start_line, suggestion.line)
        )
        if not any(_added_lines_include_exact_replacement(hunk.added_text, suggestion.replacement) for hunk in matching_hunks):
            return AcceptedSuggestionFastPathResult(False, None, "suggestion_not_applied_exactly", suggestions=suggestions)

    event: SuggestionFastPathEvent = (
        "APPROVE" if terminal_events_enabled and pr_author.lower() != bot_login.lower() else "COMMENT"
    )
    return AcceptedSuggestionFastPathResult(True, event, "accepted_suggestions_applied_exactly", suggestions=suggestions)
