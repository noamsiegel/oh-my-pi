"""Autonomous PR-review self-improvement scheduler."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

from omp_rpc import RpcClient

from robomp.config import Settings
from robomp.db import Database
from robomp.git_ops import _basic_auth_header

log = logging.getLogger(__name__)

CommandRunner = Callable[[Sequence[str], Path, float | None, Mapping[str, str] | None], subprocess.CompletedProcess[str]]

_ALLOWED_OVERALL = {"clean", "changed", "failed"}
_ALLOWED_SEVERITY = {"critical", "required", "optional"}
_ALLOWED_CATEGORY = {"streamline", "durability", "bug", "policy", "delegation", "prompt", "tooling", "learning"}

_PUSH_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


@dataclass(slots=True, frozen=True)
class QualityGate:
    name: str
    args: tuple[str, ...]
    cwd: Path
    timeout: float


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _default_command_runner(args: Sequence[str], cwd: Path, timeout: float | None, env: Mapping[str, str] | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=str(cwd), text=True, capture_output=True, check=False, timeout=timeout, env=dict(env) if env is not None else None)


def summarize_omp_jsonl(path: Path, *, max_events: int = 400) -> dict[str, Any]:
    data = path.read_bytes()[: 2 * 1024 * 1024]
    events: list[dict[str, Any]] = []
    tool_calls: list[str] = []
    errors: list[str] = []
    final_text = ""
    token_usage: dict[str, int] = {}
    for raw in data.decode("utf-8", "replace").splitlines()[:max_events]:
        try:
            event = json.loads(raw)
        except Exception:
            continue
        if isinstance(event, dict):
            slim = {k: v for k, v in event.items() if k not in {"thinking", "reasoning", "encrypted_reasoning"}}
            events.append(slim)
            text = json.dumps(slim, ensure_ascii=False)
            if "prepare_pr_review" in text: tool_calls.append("prepare_pr_review")
            if "delegate_pr_review" in text: tool_calls.append("delegate_pr_review")
            if "validate_pr_review" in text: tool_calls.append("validate_pr_review")
            if "submit_pr_review" in text: tool_calls.append("submit_pr_review")
            if slim.get("isError") is True or slim.get("error"):
                errors.append(str(slim.get("error") or slim)[:500])
            usage = slim.get("usage")
            if isinstance(usage, dict):
                for key, value in usage.items():
                    if isinstance(value, int): token_usage[key] = token_usage.get(key, 0) + value
            if slim.get("role") == "assistant" and isinstance(slim.get("content"), str):
                final_text = str(slim["content"])[-2000:]
    return {"path": str(path), "event_count": len(events), "tool_calls": tool_calls, "errors": errors, "final_assistant_text": final_text, "token_usage": token_usage}


def summarize_session_dir(session_dir: Path) -> dict[str, Any]:
    files: list[str] = []
    jsonl: list[dict[str, Any]] = []
    artifacts: dict[str, str] = {}
    if not session_dir.exists():
        return {"session_dir": str(session_dir), "files": [], "jsonl": [], "artifacts": {}}
    for path in session_dir.glob("*.jsonl"):
        jsonl.append(summarize_omp_jsonl(path))
    for path in session_dir.glob("delegated-*/*.jsonl"):
        jsonl.append(summarize_omp_jsonl(path))
    names = [
        "pr-review-evidence.json", "pr-review-classification.json", "pr-review-metadata.json", "pr-review-findings.json",
        "pr-review-validated.json", "pr-review-verify-status.json", "pr-review-payload.json",
    ]
    for path in list(session_dir.glob("pai-pr-reviewer-*.md")) + list(session_dir.glob("pai-pr-reviewer-*.json")) + list(session_dir.glob("pr-review-reviewer-*.md")) + list(session_dir.glob("pr-review-reviewer-*.json")) + list(session_dir.glob("pai-pr-*.json")) + [session_dir / name for name in names]:
        if path.is_file() and path.is_relative_to(session_dir):
            files.append(str(path.relative_to(session_dir)))
            try: artifacts[str(path.relative_to(session_dir))] = path.read_text(encoding="utf-8")[:4000]
            except Exception: pass
    return {"session_dir": str(session_dir), "files": sorted(set(files)), "jsonl": jsonl, "artifacts": artifacts}


def collect_pr_review_sessions(db: Database, *, since: str, until: str, limit: int) -> list[dict[str, Any]]:
    rows = db.list_pr_review_issue_rows_for_self_improvement(since=since, until=until, limit=limit)
    latest = db.latest_events_for_issues([row.key for row in rows])
    tool_counts = db.pr_review_tool_call_counts([row.key for row in rows])
    out: list[dict[str, Any]] = []
    for row in rows:
        event = latest.get(row.key)
        item = {"issue_key": row.key, "repo": row.repo, "number": row.number, "pr_number": row.pr_number, "updated_at": row.updated_at, "latest_event": None if event is None else {"delivery_id": event.delivery_id, "event_type": event.event_type, "state": event.state}, "tool_counts": tool_counts.get(row.key, {}), "session": summarize_session_dir(Path(row.session_dir or ""))}
        out.append(item)
    return out


def collect_pr_review_missed_gaps(db: Database, *, since: str, until: str, limit: int) -> list[dict[str, Any]]:
    rows = db.list_pr_review_gap_events_for_self_improvement(since=since, until=until, limit=limit)
    return [
        {
            "gap_id": row.gap_id,
            "repo": row.repo,
            "pr_number": row.pr_number,
            "head_sha": row.head_sha,
            "event_delivery_id": row.event_delivery_id,
            "source_object_kind": row.source_object_kind,
            "source_object_id": row.source_object_id,
            "actor_login": row.actor_login,
            "path": row.path,
            "line": row.line,
            "start_line": row.start_line,
            "body": (row.body or "")[:2000],
            "body_hash": row.body_hash,
            "severity_hint": row.severity_hint,
            "thread_id": row.thread_id,
            "confidence": row.confidence,
            "reason": row.reason,
            "created_at": row.created_at,
            "observed_at": row.observed_at,
        }
        for row in rows
    ]



def build_self_improvement_prompt(packet: Mapping[str, Any]) -> str:
    template = resources.files("robomp.prompts").joinpath("pr_review_self_improvement.md").read_text(encoding="utf-8")
    return template + json.dumps(packet, separators=(",", ":"))


def parse_self_improvement_agent_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except Exception as exc:
        raise ValueError("self-improvement agent returned non-JSON") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("self-improvement JSON has wrong schema_version")
    if data.get("overall") not in _ALLOWED_OVERALL:
        raise ValueError("self-improvement JSON has unknown overall")
    if data.get("pushed") is True:
        raise ValueError("self-improvement agent may not push")
    recs = data.get("recommendations")
    if not isinstance(recs, list):
        raise ValueError("recommendations must be an array")
    for rec in recs:
        if not isinstance(rec, dict):
            raise ValueError("recommendation must be object")
        if rec.get("severity") not in _ALLOWED_SEVERITY or rec.get("category") not in _ALLOWED_CATEGORY:
            raise ValueError("recommendation has unknown enum")
        for field in ("title", "summary", "proposed_change", "verification"):
            if not isinstance(rec.get(field), str) or not rec[field]:
                raise ValueError(f"recommendation missing {field}")
        evidence = rec.get("evidence")
        if not isinstance(evidence, list) or len(evidence) > 10:
            raise ValueError("recommendation evidence invalid")
    return data


def build_quality_gates(repo_root: Path, *, timeout: float) -> list[QualityGate]:
    run_dir = Path("/data/pr-review/self-improvements")
    kit_root = repo_root / "deploy/pr-review-kit"
    robomp_root = repo_root / "vendor/oh-my-pi/python/robomp"
    return [
        QualityGate("review-learning", ("bun", "Tools/test-review-learning.ts"), kit_root, timeout),
        QualityGate("verify-status", ("bun", "Tools/test-verify-status.ts"), kit_root, timeout),
        QualityGate("classifier", ("bun", "Tools/test-classifier.ts"), kit_root, timeout),
        QualityGate(
            "robomp-pytest",
            (
                "uv",
                "run",
                "--no-project",
                "--with-editable",
                ".",
                "--with-editable",
                "../omp-rpc",
                "--with",
                "pytest",
                "--with",
                "pytest-asyncio",
                "--with",
                "respx",
                "pytest",
                "tests/test_config.py",
                "tests/test_db.py",
                "tests/test_github_events.py",
                "tests/test_server.py",
                "tests/test_host_tools.py",
                "tests/test_github_client.py",
                "tests/test_proxy_client.py",
                "tests/test_proxy_server.py",
                "tests/test_pr_review_lifecycle.py",
                "tests/test_pr_review_self_improver.py",
            ),
            robomp_root,
            timeout,
        ),
        QualityGate(
            "learn-eval-run",
            (
                "bun",
                "Tools/pr-review-evidence.ts",
                "learn-eval-run",
                "--db",
                "/data/pr-review/review-learning.sqlite",
                "--app-db",
                "/data/sqlite/robomp.sqlite",
                "--out",
                str(run_dir / "eval-run.json"),
            ),
            kit_root,
            timeout,
        ),
    ]


class PrReviewSelfImprovementScheduler:
    def __init__(self, *, settings: Settings, db: Database, command_runner: CommandRunner | None = None, rpc_client_factory: Callable[..., Any] | None = None) -> None:
        self._settings = settings
        self._db = db
        self._command_runner = command_runner or _default_command_runner
        self._rpc_client_factory = rpc_client_factory or RpcClient
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._settings.pr_review_self_improve_enabled and self._settings.pr_review_self_improve_batch_size > 0)

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="pr-review-self-improver")

    async def stop(self) -> None:
        if self._task is None: return
        assert self._stop_event is not None
        self._stop_event.set()
        try: await asyncio.wait_for(self._task, timeout=5.0)
        except TimeoutError:
            self._task.cancel()
            try: await self._task
            except Exception: pass
        finally:
            self._task = None; self._stop_event = None

    async def _run(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try: await self.tick()
            except Exception: log.exception("PR review self-improvement tick failed")
            try: await asyncio.wait_for(self._stop_event.wait(), timeout=300.0)
            except TimeoutError: continue

    def _claim_run(self) -> tuple[str, list[Mapping[str, Any]], str, str] | None:
        until = _utcnow_iso()
        since = (datetime.now(UTC) - timedelta(days=self._settings.pr_review_self_improve_lookback_days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        claim = self._db.claim_pr_review_self_improvement_run(
            batch_size=self._settings.pr_review_self_improve_batch_size,
            scanned_since=since,
            scanned_until=until,
            model=self._settings.pr_review_self_improve_model or self._settings.pick_model(),
        )
        if claim is None:
            return None
        run_id, reviews = claim
        log.info(
            "pr_review_self_improvement_claimed",
            extra={"run_id": run_id, "session_count": 0, "recommendation_count": 0, "review_count": len(reviews)},
        )
        return run_id, reviews, since, until

    def _ensure_clean_checkout(self, *, run_id: str, repo_root: Path) -> bool:
        status = self._command_runner(["jj", "-R", str(repo_root), "status"], repo_root, 60.0, None)
        if "The working copy has no changes." in (status.stdout or ""):
            return True
        self._db.finish_pr_review_self_improvement_run(
            run_id=run_id,
            status="skipped",
            session_count=0,
            recommendation_count=0,
            error="source checkout dirty before self-improvement",
        )
        log.info(
            "pr_review_self_improvement_skipped_dirty_checkout",
            extra={"run_id": run_id, "session_count": 0, "recommendation_count": 0},
        )
        return False

    def _run_agent(
        self,
        *,
        run_id: str,
        reviews: list[Mapping[str, Any]],
        since: str,
        until: str,
        run_dir: Path,
        repo_root: Path,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        sessions = collect_pr_review_sessions(self._db, since=since, until=until, limit=self._settings.pr_review_self_improve_max_sessions)
        missed_gaps = collect_pr_review_missed_gaps(
            self._db,
            since=since,
            until=until,
            limit=self._settings.pr_review_self_improve_max_sessions,
        )
        packet = {"run_id": run_id, "reviews": reviews, "sessions": sessions, "missed_by_agent_gaps": missed_gaps}
        prompt = build_self_improvement_prompt(packet)
        try:
            with self._rpc_client_factory(executable=self._settings.omp_command, cwd=repo_root, session_dir=run_dir, env={"ROBOMP_SELF_IMPROVE": "1"}, no_session=True, no_skills=False, no_rules=False, no_title=True, model=self._settings.pr_review_self_improve_model or self._settings.pick_model(), provider=self._settings.provider, thinking=self._settings.thinking_level if self._settings.thinking_level != "off" else None, custom_tools=[], request_timeout=self._settings.request_timeout_seconds, startup_timeout=60.0) as client:
                turn = client.prompt_and_wait(prompt, timeout=self._settings.task_timeout_seconds)
                text = turn.require_assistant_text()
            return parse_self_improvement_agent_json(text), sessions
        except Exception as exc:
            self._db.finish_pr_review_self_improvement_run(
                run_id=run_id,
                status="failed",
                session_count=len(sessions),
                recommendation_count=0,
                error=str(exc),
            )
            log.exception(
                "pr_review_self_improvement_agent_failed",
                extra={"run_id": run_id, "session_count": len(sessions), "recommendation_count": 0, "error": str(exc)},
            )
            return None

    def _persist_report(self, *, run_id: str, run_dir: Path, result: Mapping[str, Any]) -> int:
        rec_count = self._db.record_pr_review_self_improvement_recommendations(run_id=run_id, recommendations=result.get("recommendations", []))
        (run_dir / "self-improvement.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        (run_dir / "self-improvement.md").write_text(str(result.get("summary") or ""), encoding="utf-8")
        return rec_count

    def _push_env(self) -> dict[str, str] | None:
        token = self._settings.pr_review_self_improve_push_token
        if token is None:
            return None
        token_value = token.get_secret_value().strip()
        if not token_value:
            return None
        env = {key: value for key in _PUSH_ENV_ALLOWLIST if (value := os.environ.get(key))}
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": _basic_auth_header(token_value),
            }
        )
        return env

    def _run_quality_gates(
        self,
        *,
        run_id: str,
        repo_root: Path,
        sessions: list[dict[str, Any]],
        recommendation_count: int,
    ) -> list[dict[str, Any]] | None:
        gate_results: list[dict[str, Any]] = []
        timeout = self._settings.pr_review_self_improve_quality_gate_timeout_seconds
        for gate in build_quality_gates(repo_root, timeout=timeout):
            cmd = list(gate.args)
            proc = self._command_runner(cmd, gate.cwd, gate.timeout, None)
            gate_results.append({"name": gate.name, "cmd": cmd, "code": proc.returncode, "stdout": (proc.stdout or "")[-1000:], "stderr": (proc.stderr or "")[-1000:]})
            if proc.returncode != 0:
                self._db.finish_pr_review_self_improvement_run(
                    run_id=run_id,
                    status="failed",
                    session_count=len(sessions),
                    recommendation_count=recommendation_count,
                    quality_gate={"status": "failed", "results": gate_results},
                    error="quality gate failed",
                )
                log.warning(
                    "pr_review_self_improvement_gate_failed",
                    extra={"run_id": run_id, "session_count": len(sessions), "recommendation_count": recommendation_count, "gate": gate.name},
                )
                return None
        return gate_results

    def _publish_change(
        self,
        *,
        run_id: str,
        repo_root: Path,
        sessions: list[dict[str, Any]],
        recommendation_count: int,
        result: Mapping[str, Any],
        gate_results: list[dict[str, Any]],
        push_env: Mapping[str, str],
        run_dir: Path,
    ) -> dict[str, Any]:
        message = str(result.get("commit_message") or "Improve PR review self-learning")
        publish_steps: tuple[tuple[list[str], Mapping[str, str] | None], ...] = (
            (["jj", "-R", str(repo_root), "describe", "-m", message], None),
            (["jj", "-R", str(repo_root), "bookmark", "set", "main", "-r", "@"], None),
            (["jj", "-R", str(repo_root), "git", "push", "--bookmark", "main"], push_env),
        )
        for cmd, env in publish_steps:
            proc = self._command_runner(cmd, repo_root, 300.0, env)
            if proc.returncode != 0:
                self._db.finish_pr_review_self_improvement_run(
                    run_id=run_id,
                    status="failed",
                    session_count=len(sessions),
                    recommendation_count=recommendation_count,
                    quality_gate={"status": "passed", "results": gate_results},
                    error=proc.stderr or proc.stdout,
                )
                return {"status": "failed", "run_id": run_id}
        log_proc = self._command_runner(["jj", "-R", str(repo_root), "log", "-r", "@", "--no-graph", "--template", "commit_id.short()"], repo_root, 60.0, None)
        commit_id = (log_proc.stdout or "").strip()
        new_proc = self._command_runner(["jj", "-R", str(repo_root), "new"], repo_root, 60.0, None)
        if new_proc.returncode != 0:
            self._db.finish_pr_review_self_improvement_run(
                run_id=run_id,
                status="failed",
                session_count=len(sessions),
                recommendation_count=recommendation_count,
                quality_gate={"status": "passed", "results": gate_results},
                error=new_proc.stderr or new_proc.stdout,
            )
            return {"status": "failed", "run_id": run_id}
        self._db.finish_pr_review_self_improvement_run(
            run_id=run_id,
            status="done",
            session_count=len(sessions),
            recommendation_count=recommendation_count,
            files_changed=len(result.get("files_changed", []) or []),
            commit_id=commit_id,
            pushed_bookmark="main",
            report_path=str(run_dir / "self-improvement.json"),
            quality_gate={"status": "passed", "results": gate_results},
        )
        log.info(
            "pr_review_self_improvement_publish_success",
            extra={"run_id": run_id, "session_count": len(sessions), "recommendation_count": recommendation_count},
        )
        return {"status": "done", "run_id": run_id, "commit_id": commit_id}

    async def tick(self) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "disabled"}
        repo_root = self._settings.pr_review_self_improve_repo_root
        claim = self._claim_run()
        if claim is None:
            return {"status": "noop"}
        run_id, reviews, since, until = claim
        run_dir = self._settings.pr_review_self_improve_report_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        if not self._ensure_clean_checkout(run_id=run_id, repo_root=repo_root):
            return {"status": "skipped", "run_id": run_id}
        agent_result = self._run_agent(run_id=run_id, reviews=reviews, since=since, until=until, run_dir=run_dir, repo_root=repo_root)
        if agent_result is None:
            return {"status": "failed", "run_id": run_id}
        result, sessions = agent_result
        rec_count = self._persist_report(run_id=run_id, run_dir=run_dir, result=result)
        changed = self._command_runner(["jj", "-R", str(repo_root), "status"], repo_root, 60.0, None)
        if "The working copy has no changes." in (changed.stdout or ""):
            self._db.finish_pr_review_self_improvement_run(run_id=run_id, status="done", session_count=len(sessions), recommendation_count=rec_count, files_changed=0, report_path=str(run_dir / "self-improvement.json"), quality_gate={"status": "passed"})
            return {"status": "done", "run_id": run_id}
        push_env = self._push_env()
        if push_env is None:
            error = "self-improvement push token not configured"
            self._db.finish_pr_review_self_improvement_run(run_id=run_id, status="failed", session_count=len(sessions), recommendation_count=rec_count, report_path=str(run_dir / "self-improvement.json"), error=error)
            return {"status": "failed", "run_id": run_id}
        gate_results = self._run_quality_gates(run_id=run_id, repo_root=repo_root, sessions=sessions, recommendation_count=rec_count)
        if gate_results is None:
            return {"status": "failed", "run_id": run_id}
        return self._publish_change(run_id=run_id, repo_root=repo_root, sessions=sessions, recommendation_count=rec_count, result=result, gate_results=gate_results, push_env=push_env, run_dir=run_dir)
