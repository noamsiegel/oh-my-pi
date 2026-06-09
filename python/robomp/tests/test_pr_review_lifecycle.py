from __future__ import annotations
import json
import subprocess

from pathlib import Path

from robomp.db import Database
from robomp.pr_review_lifecycle import (
    body_hash,
    extract_github_suggestion_hash,
    lifecycle_fact_from_payload,
    observe_webhook,
    severity_hint,
    source_label,
)


def test_lifecycle_helpers_classify_sources_and_severity() -> None:
    assert body_hash("a\r\nb") == body_hash("a\nb")
    assert source_label("copilot-pull-request-reviewer", "Bot", "") == "copilot"
    assert source_label("claude", "Bot", "") == "agent"
    assert source_label("alice", "User", "please fix") == "human"
    assert severity_hint("security critical must fix") == "critical"
    assert severity_hint("this should not break") == "required"
    assert severity_hint("nit: typo") == "optional"
    assert extract_github_suggestion_hash("```suggestion\nreturn true;\n```") is not None


def test_lifecycle_fact_extracts_review_comment() -> None:
    payload = {
        "action": "created",
        "repository": {"full_name": "octo/widget"},
        "pull_request": {"number": 12, "head": {"sha": "head-sha"}},
        "comment": {
            "id": 100,
            "path": "src/a.ts",
            "line": 10,
            "body": "Please add a test.",
            "commit_id": "comment-sha",
            "user": {"login": "alice", "type": "User"},
        },
    }
    fact = lifecycle_fact_from_payload("pull_request_review_comment", payload, delivery_id="d1")
    assert fact is not None
    assert fact["repo"] == "octo/widget"
    assert fact["object_kind"] == "review_comment"
    assert fact["head_sha"] == "comment-sha"
    assert fact["path"] == "src/a.ts"


def test_observe_webhook_records_missed_by_agent(db: Database) -> None:
    db.record_pr_review_posted_findings(
        issue_key="octo/widget#12",
        repo="octo/widget",
        pr_number=12,
        head_sha="head-sha",
        review_id=1,
        findings=[{"path": "src/a.ts", "line": 10, "body": "Agent caught this.", "severity": "required", "intent": "bug"}],
        posted_comments=[{"id": 2, "path": "src/a.ts", "line": 10, "body": "Agent caught this."}],
    )
    payload = {
        "action": "created",
        "repository": {"full_name": "octo/widget"},
        "pull_request": {"number": 12, "head": {"sha": "head-sha"}},
        "comment": {
            "id": 3,
            "path": "src/b.ts",
            "line": 20,
            "body": "Please add a regression test for the null case.",
            "commit_id": "head-sha",
            "user": {"login": "alice", "type": "User"},
        },
    }
    observe_webhook(
        db,
        event_type="pull_request_review_comment",
        delivery_id="d1",
        payload=payload,
        bot_login="robomp-bot",
        allowlist=frozenset({"octo/widget"}),
    )
    gaps = db.list_pr_review_gap_events("octo/widget", 12)
    assert len(gaps) == 1
    assert gaps[0].gap_kind == "missed_by_agent"
    assert gaps[0].path == "src/b.ts"


def test_human_review_comment_after_agent_review_exports_missed_eval(db: Database, tmp_path: Path) -> None:
    db.record_pr_review_posted_findings(
        issue_key="mainstay-io/monorepo#123",
        repo="mainstay-io/monorepo",
        pr_number=123,
        head_sha="agent-sha",
        review_id=9001,
        findings=[
            {
                "path": "src/a.ts",
                "line": 10,
                "body": "Agent caught this existing issue.",
                "severity": "required",
                "intent": "bug",
            }
        ],
        posted_comments=[{"id": 9002, "path": "src/a.ts", "line": 10, "body": "Agent caught this existing issue."}],
    )
    payload = {
        "action": "created",
        "repository": {"full_name": "mainstay-io/monorepo"},
        "pull_request": {"number": 123, "head": {"sha": "agent-sha"}},
        "comment": {
            "id": 9100,
            "path": "src/b.ts",
            "line": 20,
            "body": "Please add a regression test for the null case.",
            "commit_id": "agent-sha",
            "user": {"login": "human-reviewer", "type": "User"},
        },
    }
    observe_webhook(
        db,
        event_type="pull_request_review_comment",
        delivery_id="delivery-9100",
        payload=payload,
        bot_login="robomp-bot",
        allowlist=frozenset({"mainstay-io/monorepo"}),
    )
    gaps = db.list_pr_review_gap_events("mainstay-io/monorepo", 123)
    assert len(gaps) == 1
    assert gaps[0].gap_kind == "missed_by_agent"
    assert gaps[0].path == "src/b.ts"

    helper_db = tmp_path / "helper.sqlite"
    scan = tmp_path / "scan.json"
    evals = tmp_path / "evals.jsonl"
    helper = Path("/Users/mac/robo-ms/deploy/pr-review-kit/Tools/pr-review-evidence.ts")
    cwd = helper.parents[1]
    subprocess.run(
        ["bun", str(helper), "learn-gap-scan", "--app-db", str(db.path), "--db", str(helper_db), "--out", str(scan)],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        ["bun", str(helper), "learn-eval-export", "--db", str(helper_db), "--out", str(evals)],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    exported = [json.loads(line) for line in evals.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert exported[0]["item"]["case_kind"] == "missed_by_agent"
    assert exported[0]["item"]["expected"]["path"] == "src/b.ts"
    assert exported[0]["item"]["expected"]["theme"] == "test_gap"
