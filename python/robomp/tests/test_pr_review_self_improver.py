from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from robomp.config import Settings
from robomp.db import Database
from robomp.pr_review_self_improver import (
    PrReviewSelfImprovementScheduler,
    build_quality_gates,
    parse_self_improvement_agent_json,
    summarize_session_dir,
)


class FakeRpc:
    prompts: list[str] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.text = json.dumps(
            {
                "schema_version": 1,
                "overall": "changed",
                "summary": "tightened reviewer validation",
                "recommendations": [
                    {
                        "severity": "required",
                        "category": "durability",
                        "title": "Validate delegated reviewer output",
                        "summary": "Ensure bad reviewer YAML is rejected.",
                        "evidence": ["octo/widget#1 pr-review-validated.json"],
                        "proposed_change": "Add validation.",
                        "verification": "pytest",
                    }
                ],
                "files_changed": ["vendor/oh-my-pi/python/robomp/src/host_tools.py"],
                "quality_gates": [],
                "commit_message": "Improve PR review self-learning",
                "pushed": False,
            }
        )

    def __enter__(self) -> FakeRpc:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def prompt_and_wait(self, prompt: str, timeout: float) -> Any:
        self.prompts.append(prompt)
        assert "Robo-MS PR-review self-improvement agent" in prompt
        return SimpleNamespace(require_assistant_text=lambda: self.text)


def _settings(tmp_path: Path) -> Settings:
    return Settings.model_construct(
        github_token=None,
        github_webhook_secret=SecretStr("x"),
        bot_login="robomp-bot",
        git_author_email="robomp-bot@example.invalid",
        repo_allowlist_raw="octo/widget",
        gh_proxy_url="http://proxy",
        gh_proxy_hmac_key=SecretStr("hmac"),
        workspace_root=tmp_path / "workspaces",
        sqlite_path=tmp_path / "db.sqlite",
        log_dir=tmp_path / "logs",
        omp_command="omp",
        model="openai-codex/gpt-5.5",
        provider=None,
        thinking_level="low",
        task_timeout_seconds=30.0,
        request_timeout_seconds=30.0,
        pr_review_self_improve_enabled=True,
        pr_review_self_improve_batch_size=10,
        pr_review_self_improve_lookback_days=30,
        pr_review_self_improve_max_sessions=50,
        pr_review_self_improve_report_dir=tmp_path / "reports",
        pr_review_self_improve_model="",
        pr_review_self_improve_repo_root=tmp_path / "repo",
        pr_review_self_improve_quality_gate_timeout_seconds=30.0,
        pr_review_self_improve_push_token=SecretStr("push-token"),
    )


def _seed_reviews(db: Database, n: int) -> None:
    for i in range(n):
        db.record_pr_review_completed_review(
            issue_key=f"octo/widget#{i}",
            repo="octo/widget",
            pr_number=i,
            head_sha=f"sha-{i}",
            github_review_id=1000 + i,
            event="COMMENT",
        )


def test_summarizer_ignores_encrypted_thinking_and_captures_tool_sequence(tmp_path: Path) -> None:
    session = tmp_path / ".omp-session"
    session.mkdir()
    (session / "main.jsonl").write_text(
        '\n'.join([
            json.dumps({"role": "assistant", "thinking": "secret", "content": "prepare_pr_review delegate_pr_review validate_pr_review submit_pr_review"}),
            json.dumps({"tool": "submit_pr_review", "isError": False}),
        ]),
        encoding="utf-8",
    )
    (session / "pai-pr-reviewer-tests.md").write_text("overall: correct", encoding="utf-8")
    summary = summarize_session_dir(session)
    assert "pai-pr-reviewer-tests.md" in summary["files"]
    assert "secret" not in json.dumps(summary)
    assert "submit_pr_review" in summary["jsonl"][0]["tool_calls"]


def test_parse_self_improvement_agent_json_rejects_invalid() -> None:
    valid = json.loads(FakeRpc().text)
    assert parse_self_improvement_agent_json(json.dumps(valid))["overall"] == "changed"
    with pytest.raises(ValueError):
        parse_self_improvement_agent_json("not json")
    invalid = dict(valid, overall="weird")
    with pytest.raises(ValueError):
        parse_self_improvement_agent_json(json.dumps(invalid))
    invalid = dict(valid, pushed=True)
    with pytest.raises(ValueError):
        parse_self_improvement_agent_json(json.dumps(invalid))

def test_quality_gates_use_expected_cwd() -> None:
    repo_root = Path("/source/robo-ms")
    gates = build_quality_gates(repo_root, timeout=12.5)
    assert [gate.cwd for gate in gates] == [
        repo_root / "deploy/pr-review-kit",
        repo_root / "deploy/pr-review-kit",
        repo_root / "deploy/pr-review-kit",
        repo_root / "vendor/oh-my-pi/python/robomp",
        repo_root / "deploy/pr-review-kit",
    ]
    assert all(gate.timeout == 12.5 for gate in gates)
    assert [list(gate.args)[:2] for gate in gates] == [
        ["bun", "Tools/test-review-learning.ts"],
        ["bun", "Tools/test-verify-status.ts"],
        ["bun", "Tools/test-classifier.ts"],
        ["uv", "run"],
        ["bun", "Tools/pr-review-evidence.ts"],
    ]


@pytest.mark.asyncio
async def test_self_improver_disabled_does_not_need_push_token(db: Database, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.pr_review_self_improve_enabled = False
    settings.pr_review_self_improve_push_token = None
    calls: list[list[str]] = []
    def runner(args: Sequence[str], cwd: Path, timeout: float | None, env: Mapping[str, str] | None):
        calls.append(list(args))
        return subprocess.CompletedProcess(list(args), 0, "ok", "")
    sched = PrReviewSelfImprovementScheduler(settings=settings, db=db, command_runner=runner, rpc_client_factory=FakeRpc)
    assert await sched.tick() == {"status": "disabled"}
    assert calls == []




@pytest.mark.asyncio
async def test_self_improver_noops_before_ten_reviews(db: Database, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.pr_review_self_improve_repo_root.mkdir(parents=True)
    _seed_reviews(db, 9)
    calls: list[list[str]] = []
    def runner(args: Sequence[str], cwd: Path, timeout: float | None, env: Mapping[str, str] | None):
        calls.append(list(args))
        return subprocess.CompletedProcess(list(args), 0, "The working copy has no changes.", "")
    sched = PrReviewSelfImprovementScheduler(settings=settings, db=db, command_runner=runner, rpc_client_factory=FakeRpc)
    assert await sched.tick() == {"status": "noop"}
    assert calls == []


@pytest.mark.asyncio
async def test_self_improver_runs_after_ten_reviews_and_pushes_only_after_gates(db: Database, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.pr_review_self_improve_repo_root.mkdir(parents=True)
    _seed_reviews(db, 10)
    calls: list[list[str]] = []
    status_calls = 0
    push_envs: list[Mapping[str, str] | None] = []
    def runner(args: Sequence[str], cwd: Path, timeout: float | None, env: Mapping[str, str] | None):
        nonlocal status_calls
        cmd = list(args)
        calls.append(cmd)
        if cmd[3:] == ["git", "push", "--bookmark", "main"]:
            push_envs.append(env)
        if cmd[:3] == ["jj", "-R", str(settings.pr_review_self_improve_repo_root)] and cmd[3] == "status":
            status_calls += 1
            out = "The working copy has no changes." if status_calls == 1 else "Modified files: src/x.py"
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd[:2] == ["bun", "Tools/pr-review-evidence.ts"] and "learn-eval-run" in cmd:
            return subprocess.CompletedProcess(cmd, 0, '{"status":"passed","checked_cases":0,"failures":[]}', "")
        if cmd[0] in {"bun", "uv"}:
            return subprocess.CompletedProcess(cmd, 0, "ok", "")
        if cmd[:4] == ["jj", "-R", str(settings.pr_review_self_improve_repo_root), "log"]:
            return subprocess.CompletedProcess(cmd, 0, "abc1234", "")
        return subprocess.CompletedProcess(cmd, 0, "ok", "")
    sched = PrReviewSelfImprovementScheduler(settings=settings, db=db, command_runner=runner, rpc_client_factory=FakeRpc)
    result = await sched.tick()
    assert result["status"] == "done"
    jj = [c for c in calls if c[:2] == ["jj", "-R"]]
    assert [c[3:] for c in jj][-5:] == [
        ["describe", "-m", "Improve PR review self-learning"],
        ["bookmark", "set", "main", "-r", "@"],
        ["git", "push", "--bookmark", "main"],
        ["log", "-r", "@", "--no-graph", "--template", "commit_id.short()"],
        ["new"],
    ]
    assert push_envs and push_envs[0] is not None
    assert push_envs[0]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert push_envs[0]["GIT_TERMINAL_PROMPT"] == "0"
    assert "push-token" not in str(push_envs[0])
    latest = db.latest_pr_review_self_improvement_run()
    assert latest is not None
    assert latest["status"] == "done"
    assert latest["commit_id"] == "abc1234"
    assert latest["pushed_bookmark"] == "main"



@pytest.mark.asyncio
async def test_self_improver_prompt_includes_missed_by_agent_gaps(db: Database, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.pr_review_self_improve_repo_root.mkdir(parents=True)
    _seed_reviews(db, 10)
    db.record_pr_review_gap_event(
        gap_kind="missed_by_agent",
        repo="octo/widget",
        pr_number=42,
        head_sha="sha-42",
        source_label="human",
        source_object_kind="review_comment",
        source_object_id="9001",
        severity_hint="required",
        confidence=0.95,
        reason="external reviewer caught normalized id drift",
        path="apps/api/view.py",
        line=123,
        body="task_id should use canonical task.id",
        event_delivery_id="delivery-1",
    )
    calls: list[list[str]] = []

    def runner(args: Sequence[str], cwd: Path, timeout: float | None, env: Mapping[str, str] | None):
        cmd = list(args)
        calls.append(cmd)
        if cmd[:3] == ["jj", "-R", str(settings.pr_review_self_improve_repo_root)] and cmd[3] == "status":
            return subprocess.CompletedProcess(cmd, 0, "The working copy has no changes.", "")
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    FakeRpc.prompts = []
    sched = PrReviewSelfImprovementScheduler(settings=settings, db=db, command_runner=runner, rpc_client_factory=FakeRpc)
    await sched.tick()
    packet = json.loads("{" + FakeRpc.prompts[-1].rsplit("\n{", 1)[1])
    gap = packet["missed_by_agent_gaps"][0]
    assert gap["event_delivery_id"] == "delivery-1"
    assert gap["path"] == "apps/api/view.py"
    assert gap["severity_hint"] == "required"

@pytest.mark.asyncio
async def test_self_improver_dirty_checkout_skips(db: Database, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.pr_review_self_improve_repo_root.mkdir(parents=True)
    _seed_reviews(db, 10)
    def runner(args: Sequence[str], cwd: Path, timeout: float | None, env: Mapping[str, str] | None):
        return subprocess.CompletedProcess(list(args), 0, "Modified files", "")
    sched = PrReviewSelfImprovementScheduler(settings=settings, db=db, command_runner=runner, rpc_client_factory=FakeRpc)
    result = await sched.tick()
    assert result["status"] == "skipped"
    latest = db.latest_pr_review_self_improvement_run()
    assert latest is not None and latest["status"] == "skipped"


@pytest.mark.asyncio
async def test_self_improver_failing_gate_does_not_push(db: Database, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.pr_review_self_improve_repo_root.mkdir(parents=True)
    _seed_reviews(db, 10)
    status_calls = 0
    calls: list[list[str]] = []
    def runner(args: Sequence[str], cwd: Path, timeout: float | None, env: Mapping[str, str] | None):
        nonlocal status_calls
        cmd = list(args)
        calls.append(cmd)
        if cmd[3:] == ["status"]:
            status_calls += 1
            return subprocess.CompletedProcess(cmd, 0, "The working copy has no changes." if status_calls == 1 else "Modified files", "")
        if cmd[0] == "bun":
            return subprocess.CompletedProcess(cmd, 1, "", "failed")
        return subprocess.CompletedProcess(cmd, 0, "ok", "")
    sched = PrReviewSelfImprovementScheduler(settings=settings, db=db, command_runner=runner, rpc_client_factory=FakeRpc)
    result = await sched.tick()
    assert result["status"] == "failed"
    assert not any(c[3:] == ["git", "push", "--bookmark", "main"] for c in calls if c[:2] == ["jj", "-R"])


@pytest.mark.asyncio
async def test_self_improver_missing_push_token_fails_before_gates(db: Database, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.pr_review_self_improve_push_token = None
    settings.pr_review_self_improve_repo_root.mkdir(parents=True)
    _seed_reviews(db, 10)
    status_calls = 0
    calls: list[list[str]] = []
    def runner(args: Sequence[str], cwd: Path, timeout: float | None, env: Mapping[str, str] | None):
        nonlocal status_calls
        cmd = list(args)
        calls.append(cmd)
        if cmd[3:] == ["status"]:
            status_calls += 1
            return subprocess.CompletedProcess(cmd, 0, "The working copy has no changes." if status_calls == 1 else "Modified files", "")
        return subprocess.CompletedProcess(cmd, 0, "ok", "")
    sched = PrReviewSelfImprovementScheduler(settings=settings, db=db, command_runner=runner, rpc_client_factory=FakeRpc)
    result = await sched.tick()
    assert result["status"] == "failed"
    assert not any(c and c[0] in {"bun", "uv"} for c in calls)
    assert not any(c[3:] == ["git", "push", "--bookmark", "main"] for c in calls if c[:2] == ["jj", "-R"])
    latest = db.latest_pr_review_self_improvement_run()
    assert latest is not None
    assert latest["error"] == "self-improvement push token not configured"
