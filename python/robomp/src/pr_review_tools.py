"""Helpers for PR-review host tools."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omp_rpc import RpcCommandError

from robomp.config import Settings
from robomp.sandbox import Workspace, _prepare_slot_runtime_env, _safe_directory_env, _slot_subprocess_kwargs

if TYPE_CHECKING:
    from robomp.host_tools import ToolBindings

_REPO_COMMAND_PARENT_ENV_KEYS: frozenset[str] = frozenset({"PATH", "LANG", "LC_ALL"})
_AGENT_HOME = Path("/srv/agent-home")


@dataclass(slots=True, frozen=True)
class PrReviewPaths:
    evidence: Path
    classification: Path
    metadata: Path
    gate: Path
    findings: Path
    validated: Path
    verify_status: Path
    payload: Path
    body: Path


@dataclass(slots=True, frozen=True)
class ReviewFinding:
    path: str
    line: int
    body: str
    severity: str
    intent: str
    start_line: int | None = None
    suggestion: Mapping[str, Any] | None = None


def pr_review_paths(workspace: Workspace) -> PrReviewPaths:
    session = workspace.session_dir
    return PrReviewPaths(
        evidence=session / "pr-review-evidence.json",
        classification=session / "pr-review-classification.json",
        metadata=session / "pr-review-metadata.json",
        gate=session / "pr-review-gate.json",
        findings=session / "pr-review-findings.json",
        validated=session / "pr-review-validated.json",
        verify_status=session / "pr-review-verify-status.json",
        payload=session / "pr-review-payload.json",
        body=session / "pr-review-body.md",
    )


def load_json_checked(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        msg = f"failed to read JSON file {path}: {exc}"
        raise RpcCommandError(msg, error={"message": msg}) from exc
    except json.JSONDecodeError as exc:
        msg = f"failed to parse JSON file {path}: {exc}"
        raise RpcCommandError(msg, error={"message": msg}) from exc


def save_json_checked(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        msg = f"failed to write JSON file {path}: {exc}"
        raise RpcCommandError(msg, error={"message": msg}) from exc


def pr_review_helper_path(settings: Settings | None) -> Path | None:
    if settings is None or settings.pr_review_helper is None:
        return None
    helper = settings.pr_review_helper
    return helper if helper.is_file() else None


def pr_review_parse_diff_anchors(diff: str) -> list[dict[str, Any]]:
    files: dict[str, list[dict[str, Any]]] = {}
    current_path: str | None = None
    right_line = 0
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            current_path = None
            continue
        if raw.startswith("+++ b/"):
            current_path = raw.removeprefix("+++ b/")
            files.setdefault(current_path, [])
            continue
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if hunk is not None:
            right_line = int(hunk.group(1))
            continue
        if not current_path or raw.startswith(("--- ", "+++ ")):
            continue
        if raw.startswith("+"):
            files[current_path].append({"line": right_line, "kind": "added"})
            right_line += 1
        elif raw.startswith(" "):
            files[current_path].append({"line": right_line, "kind": "context"})
            right_line += 1
        elif raw.startswith("-") or raw == "\\ No newline at end of file":
            continue
    anchors: list[dict[str, Any]] = []
    for path, right_lines in files.items():
        valid = sorted({int(line["line"]) for line in right_lines})
        added = sorted({int(line["line"]) for line in right_lines if line["kind"] == "added"})
        anchors.append({"path": path, "rightLines": right_lines, "validRightLines": valid, "addedRightLines": added})
    return anchors


def pr_review_model_family(model: str | None) -> str | None:
    if not model:
        return None
    lowered = model.lower()
    if "anthropic" in lowered or "claude" in lowered:
        return "anthropic"
    if "openai" in lowered or "gpt" in lowered or "codex" in lowered:
        return "openai"
    if "google" in lowered or "gemini" in lowered:
        return "google"
    return None


def pr_review_delegate_models(settings: Settings | None, *, domain: str, reviewer: str) -> tuple[str | None, ...]:
    if settings is None:
        return (None,)
    model_map = settings.pr_review_delegate_model_map
    preferred = model_map.get(reviewer.strip().lower()) or model_map.get(domain.strip().lower()) or model_map.get("default")
    candidates: list[str | None] = []
    if preferred:
        candidates.append(preferred)
    configured = settings.pr_review_delegate_models or (settings.model,)
    candidates.extend(configured)
    candidates.append(settings.model)
    deduped: list[str | None] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate or ""
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return tuple(deduped)


def _format_process_output(stdout: Any, stderr: Any) -> str:
    parts: list[str] = []
    if stdout:
        parts.append(str(stdout).strip())
    if stderr:
        parts.append(str(stderr).strip())
    return "\n".join(part for part in parts if part) or "(no output)"


def pr_review_runtime_env(settings: Settings | None) -> dict[str, str]:
    """Non-secret Django env exposing the local review DB/Redis sidecars.

    Only populated for incoming-PR review sandboxes that have an HOA DB host
    configured. The credentials describe the throwaway review database only;
    no GitHub tokens or production secrets are ever included here.
    """
    if settings is None or not settings.pr_review_hoa_db_host.strip():
        return {}
    env = {
        "ENV": "dev",
        "IS_LOCAL": "1",
        "DB_NAME": settings.pr_review_hoa_db_name,
        "DB_USER": settings.pr_review_hoa_db_user,
        "DB_PASS": settings.pr_review_hoa_db_pass,
        "DB_HOST": settings.pr_review_hoa_db_host,
        "DB_PORT": settings.pr_review_hoa_db_port,
    }
    redis_host = settings.pr_review_hoa_redis_host.strip()
    if redis_host:
        env["REDIS_HOST"] = redis_host
    return env


_COMMENT_REVIEW_RETRYABLE_MARKERS = (
    "promotedsection is not defined",
    "command not found: uv",
    "`uv` is not installed",
    "`uv` is unavailable",
    "uv is not installed",
    "uv is unavailable",
    "manage.py`/`yarn` unavailable",
    "manage.py/yarn unavailable",
    "yarn unavailable",
    "command not found: yarn",
)


def pr_review_comment_retryable(review_body: str) -> bool:
    """True when a prior COMMENT review reflects a transient tooling failure.

    Such reviews are not a real verdict, so they must not anchor an incremental
    verify-fixes pass: keep doing full reviews until a genuine review lands.
    """
    body = review_body.lower()
    return any(marker in body for marker in _COMMENT_REVIEW_RETRYABLE_MARKERS)


def _repo_command_env(bindings: ToolBindings, *, include_auth_broker: bool = False) -> dict[str, str]:
    env: dict[str, str] = {
        key: value
        for key, value in os.environ.items()
        if key in _REPO_COMMAND_PARENT_ENV_KEYS
        or key.startswith("LC_")
        or (include_auth_broker and key.startswith("OMP_AUTH_BROKER_"))
    }
    if _AGENT_HOME.is_dir():
        env["HOME"] = str(_AGENT_HOME)
    env.update(_prepare_slot_runtime_env(bindings.workspace, bindings.slot_uid))
    env.update(_safe_directory_env(bindings.workspace.repo_dir))
    env.update(
        {
            "GIT_AUTHOR_NAME": bindings.author_name,
            "GIT_AUTHOR_EMAIL": bindings.author_email,
            "GIT_COMMITTER_NAME": bindings.author_name,
            "GIT_COMMITTER_EMAIL": bindings.author_email,
        }
    )
    if bindings.review_mode:
        env.update(pr_review_runtime_env(bindings.settings))
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run_pr_review_helper(
    bindings: ToolBindings,
    helper: Path,
    args: Sequence[str | Path],
    timeout: float | None,
    error_prefix: str,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["bun", str(helper), *(str(arg) for arg in args)],
        cwd=str(bindings.workspace.repo_dir),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_repo_command_env(bindings),
        **_slot_subprocess_kwargs(bindings.slot_uid),
    )
    if proc.returncode != 0:
        output = _format_process_output(proc.stdout, proc.stderr)
        msg = f"{error_prefix}: {output}"
        raise RpcCommandError(msg, error={"message": msg, "output": output})
    return proc


__all__ = [
    "PrReviewPaths",
    "ReviewFinding",
    "load_json_checked",
    "pr_review_delegate_models",
    "pr_review_helper_path",
    "pr_review_model_family",
    "pr_review_parse_diff_anchors",
    "pr_review_paths",
    "pr_review_comment_retryable",
    "pr_review_runtime_env",
    "run_pr_review_helper",
    "save_json_checked",
]
