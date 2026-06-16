"""Env-driven configuration for roboomp."""

from __future__ import annotations

import random
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ThinkingLevel = Literal["off", "low", "medium", "high", "xhigh"]


class Settings(BaseSettings):
    """Strongly-typed runtime configuration.

    Loaded from process env, optionally pre-populated by `.env`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # GitHub webhook/orchestrator identity
    github_webhook_secret: SecretStr = Field(..., alias="GITHUB_WEBHOOK_SECRET")
    bot_login: str = Field(..., alias="ROBOMP_BOT_LOGIN")
    git_author_name: str | None = Field(None, alias="ROBOMP_GIT_AUTHOR_NAME")
    git_author_email: str = Field(..., alias="ROBOMP_GIT_AUTHOR_EMAIL")
    repo_allowlist_raw: str = Field("", alias="ROBOMP_REPO_ALLOWLIST")
    pr_review_enabled: bool = Field(True, alias="ROBOMP_PR_REVIEW_ENABLED")
    pr_review_label_allowlist_raw: str = Field("", alias="ROBOMP_PR_REVIEW_LABEL_ALLOWLIST")
    pr_review_reconciler_enabled: bool = Field(True, alias="ROBOMP_PR_REVIEW_RECONCILER_ENABLED")
    pr_review_reconciler_interval_seconds: float = Field(300.0, alias="ROBOMP_PR_REVIEW_RECONCILER_INTERVAL_SECONDS")
    pr_review_reconciler_limit_per_repo: int = Field(50, alias="ROBOMP_PR_REVIEW_RECONCILER_LIMIT_PER_REPO")
    pr_review_terminal_events: bool = Field(False, alias="ROBOMP_PR_REVIEW_TERMINAL_EVENTS")
    pr_review_ci_gate_enabled: bool = Field(False, alias="ROBOMP_PR_REVIEW_CI_GATE_ENABLED")
    pr_review_ci_gate_retry_seconds: float = Field(300.0, alias="ROBOMP_PR_REVIEW_CI_GATE_RETRY_SECONDS")
    pr_review_ci_gate_timeout_seconds: float = Field(7200.0, alias="ROBOMP_PR_REVIEW_CI_GATE_TIMEOUT_SECONDS")
    # CI-gate hardening. The gate notice ("won't review until CI passes") is
    # posted at most once per *blocking episode* (a contiguous run of red CI),
    # not once per failing head — so an author iterating on a red PR no longer
    # collects one bot comment per push. ``started_comments_enabled`` controls
    # the legacy "reviewing now" ping (the review itself is the signal; off by
    # default). ``ci_preflight`` routes CI-gated discovery through the cheap
    # ``probe_pr_review_ci`` task so a full ``review_pr`` only runs once CI is
    # green. ``gate_sticky_comment`` edits the single episode comment in place
    # as counts change instead of staying stale. Backstop re-probes a blocked/
    # pending head no more often than ``ci_pending_backstop_seconds``.
    pr_review_started_comments_enabled: bool = Field(False, alias="ROBOMP_PR_REVIEW_STARTED_COMMENTS_ENABLED")
    pr_review_ci_preflight_enabled: bool = Field(True, alias="ROBOMP_PR_REVIEW_CI_PREFLIGHT_ENABLED")
    pr_review_gate_sticky_comment_enabled: bool = Field(True, alias="ROBOMP_PR_REVIEW_GATE_STICKY_COMMENT_ENABLED")
    pr_review_ci_pending_backstop_seconds: float = Field(900.0, alias="ROBOMP_PR_REVIEW_CI_PENDING_BACKSTOP_SECONDS")
    pr_review_ci_debounce_seconds: float = Field(45.0, alias="ROBOMP_PR_REVIEW_CI_DEBOUNCE_SECONDS")
    pr_review_webhook_staleness_warn_seconds: float = Field(1800.0, alias="ROBOMP_PR_REVIEW_WEBHOOK_STALENESS_WARN_SECONDS")
    pr_review_helper: Path | None = Field(None, alias="ROBOMP_PR_REVIEW_HELPER")
    pr_review_delegate_models_raw: str = Field("", alias="ROBOMP_PR_REVIEW_DELEGATE_MODELS")
    pr_review_delegate_model_map_raw: str = Field("", alias="ROBOMP_PR_REVIEW_DELEGATE_MODEL_MAP")
    pr_review_hoa_db_host: str = Field("", alias="ROBOMP_PR_REVIEW_HOA_DB_HOST")
    pr_review_hoa_db_name: str = Field("postgres", alias="ROBOMP_PR_REVIEW_HOA_DB_NAME")
    pr_review_hoa_db_user: str = Field("postgres", alias="ROBOMP_PR_REVIEW_HOA_DB_USER")
    pr_review_hoa_db_pass: str = Field("postgres", alias="ROBOMP_PR_REVIEW_HOA_DB_PASS")
    pr_review_hoa_db_port: str = Field("5432", alias="ROBOMP_PR_REVIEW_HOA_DB_PORT")
    pr_review_hoa_redis_host: str = Field("", alias="ROBOMP_PR_REVIEW_HOA_REDIS_HOST")

    pr_review_learning_db: Path | None = Field(None, alias="ROBOMP_PR_REVIEW_LEARNING_DB")
    pr_review_self_improve_enabled: bool = Field(True, alias="ROBOMP_PR_REVIEW_SELF_IMPROVE_ENABLED")
    pr_review_self_improve_batch_size: int = Field(10, alias="ROBOMP_PR_REVIEW_SELF_IMPROVE_BATCH_SIZE")
    pr_review_self_improve_lookback_days: int = Field(30, alias="ROBOMP_PR_REVIEW_SELF_IMPROVE_LOOKBACK_DAYS")
    pr_review_self_improve_max_sessions: int = Field(50, alias="ROBOMP_PR_REVIEW_SELF_IMPROVE_MAX_SESSIONS")
    pr_review_self_improve_report_dir: Path = Field(Path("/data/pr-review/self-improvements"), alias="ROBOMP_PR_REVIEW_SELF_IMPROVE_REPORT_DIR")
    pr_review_self_improve_model: str = Field("", alias="ROBOMP_PR_REVIEW_SELF_IMPROVE_MODEL")
    pr_review_self_improve_repo_root: Path = Field(Path("/source/robo-ms"), alias="ROBOMP_PR_REVIEW_SELF_IMPROVE_REPO_ROOT")
    pr_review_self_improve_push_token: SecretStr | None = Field(None, alias="ROBOMP_PR_REVIEW_SELF_IMPROVE_PUSH_TOKEN")
    pr_review_self_improve_quality_gate_timeout_seconds: float = Field(
        3600.0,
        alias="ROBOMP_PR_REVIEW_SELF_IMPROVE_QUALITY_GATE_TIMEOUT_SECONDS",
    )
    # gh-proxy process knobs. Orchestrator/proxy credentials live in
    # OrchestratorSettings and ProxySettings respectively.
    # Bind address for `python -m robomp.proxy serve`. Internal-only by
    # default; gh-proxy never exposes a host port.
    gh_proxy_bind_host: str = Field("0.0.0.0", alias="ROBOMP_GH_PROXY_BIND_HOST")
    gh_proxy_bind_port: int = Field(8081, alias="ROBOMP_GH_PROXY_BIND_PORT")

    # gh-proxy: maximum request body size (bytes). Bodies larger than this
    # are rejected with 413 BEFORE the proxy reads them into memory. Tight
    # by design — every typed endpoint payload fits in a few KB.
    gh_proxy_max_body_bytes: int = Field(1 << 20, alias="ROBOMP_GH_PROXY_MAX_BODY_BYTES")
    # Hard wall-clock budget (seconds) for a single git subprocess invoked
    # by gh-proxy. Bounds how long a hung git can pin a request handler.
    gh_proxy_git_timeout_seconds: float = Field(60.0, alias="ROBOMP_GH_PROXY_GIT_TIMEOUT_SECONDS")

    # Model selection
    model: str = Field("anthropic/claude-sonnet-4-6", alias="ROBOMP_MODEL")
    provider: str | None = Field(None, alias="ROBOMP_PROVIDER")
    thinking_level: ThinkingLevel = Field("high", alias="ROBOMP_THINKING")
    labels_comments_only: bool = Field(False, alias="ROBOMP_LABELS_COMMENTS_ONLY")

    # Runtime
    max_concurrency: int = Field(8, alias="ROBOMP_MAX_CONCURRENCY")
    task_timeout_seconds: float = Field(2400.0, alias="ROBOMP_TASK_TIMEOUT_SECONDS")
    task_timeout_hard_grace_seconds: float = Field(60.0, alias="ROBOMP_TASK_TIMEOUT_HARD_GRACE_SECONDS")
    request_timeout_seconds: float = Field(120.0, alias="ROBOMP_REQUEST_TIMEOUT_SECONDS")
    # Premature-end reminder. When a `triage_issue` turn ends without the
    # agent having reached a terminal tool (`gh_open_pr`,
    # `mark_unable_to_reproduce`, `abort_task`) for a `bug`/`documentation`
    # classification, the driver sends up to this many "you stopped before
    # opening a PR — continue" reminder prompts into the same omp session.
    # Set to 0 to disable.
    task_completion_max_reminders: int = Field(2, alias="ROBOMP_TASK_COMPLETION_MAX_REMINDERS")
    omp_command: str = Field("omp", alias="ROBOMP_OMP_COMMAND")

    # Graceful shutdown (Phase B). On SIGTERM the dispatcher stops claiming
    # new work, then waits up to `drain` seconds for in-flight events to
    # complete cleanly; any still running after that get their omp
    # subprocess killed and the row left in `running` so it requeues on
    # next start. Sum of both MUST stay below the compose `stop_grace_period`.
    shutdown_drain_timeout_seconds: float = Field(25.0, alias="ROBOMP_SHUTDOWN_DRAIN_TIMEOUT_SECONDS")
    shutdown_kill_timeout_seconds: float = Field(5.0, alias="ROBOMP_SHUTDOWN_KILL_TIMEOUT_SECONDS")

    # Paths
    workspace_root: Path = Field(Path("./data/workspaces"), alias="ROBOMP_WORKSPACE_ROOT")
    sqlite_path: Path = Field(Path("./data/robomp.sqlite"), alias="ROBOMP_SQLITE_PATH")
    log_dir: Path = Field(Path("./data/logs"), alias="ROBOMP_LOG_DIR")

    # Server
    bind_host: str = Field("0.0.0.0", alias="ROBOMP_BIND_HOST")
    bind_port: int = Field(8080, alias="ROBOMP_BIND_PORT")
    webhook_max_body_bytes: int = Field(1 << 20, alias="ROBOMP_WEBHOOK_MAX_BODY_BYTES")
    # Dev-only replay header value; if empty, /replay is disabled
    replay_token: SecretStr | None = Field(None, alias="ROBOMP_REPLAY_TOKEN")

    # Per-submitter rate limiting. `window_seconds` defines the rolling window;
    # `default` is the per-window cap for unknown/first-time submitters;
    # `contributor` is the cap for accounts whose GitHub author_association is
    # `CONTRIBUTOR` (i.e. already has a merged PR). `unlimited_raw` is a
    # comma-separated allowlist of logins that bypass the limiter entirely;
    # accounts with author_association OWNER/MEMBER/COLLABORATOR also bypass.
    rate_limit_window_seconds: float = Field(3600.0, alias="ROBOMP_RATE_LIMIT_WINDOW_SECONDS")
    rate_limit_default: int = Field(3, alias="ROBOMP_RATE_LIMIT_DEFAULT")
    rate_limit_contributor: int = Field(10, alias="ROBOMP_RATE_LIMIT_CONTRIBUTOR")
    rate_limit_unlimited_raw: str = Field("", alias="ROBOMP_RATE_LIMIT_UNLIMITED")
    # Logins (comma-separated, `@` prefix optional) whose `@bot_login`
    # mentions are treated as authoritative directives. These accounts also
    # bypass rate limiting regardless of `author_association`.
    maintainer_logins_raw: str = Field("", alias="ROBOMP_MAINTAINER_LOGINS")
    # Bot logins (e.g. chatgpt-codex-connector) whose comments/reviews are
    # treated as authoritative directives without requiring an `@bot` mention.
    # Comma-separated; `@` prefix optional.
    reviewer_bots_raw: str = Field("", alias="ROBOMP_REVIEWER_BOTS")

    # Question auto-close. When the bot answers an issue classified as
    # `question`, the comment is suffixed with a 👎-to-keep-open prompt and a
    # row is scheduled in `pending_closures`. The scheduler closes the issue
    # after `question_autoclose_hours` unless the issue author downvoted the
    # comment, a human follow-up arrived, or the issue was closed externally.
    # Set `question_autoclose_enabled=False` (or hours <= 0) to disable.
    question_autoclose_enabled: bool = Field(True, alias="ROBOMP_QUESTION_AUTOCLOSE_ENABLED")
    question_autoclose_hours: float = Field(4.0, alias="ROBOMP_QUESTION_AUTOCLOSE_HOURS")
    question_autoclose_scan_seconds: float = Field(60.0, alias="ROBOMP_QUESTION_AUTOCLOSE_SCAN_SECONDS")

    # pi-natives build-output cache. Hardlinks pre-built
    # `packages/natives/native/*.node` (and its companions) into new
    # workspaces keyed by the git tree-hashes of inputs that determine the
    # build output. Misses are captured automatically when a task that
    # finishes successfully has fresh artifacts. Disable to fall back to
    # per-workspace builds.
    natives_cache_enabled: bool = Field(True, alias="ROBOMP_NATIVES_CACHE_ENABLED")
    natives_cache_root: Path = Field(Path("/data/cache/pi-natives"), alias="ROBOMP_NATIVES_CACHE_ROOT")
    natives_cache_max_entries_per_repo: int = Field(8, alias="ROBOMP_NATIVES_CACHE_MAX_ENTRIES_PER_REPO")
    natives_cache_max_bytes: int = Field(4 * 1024**3, alias="ROBOMP_NATIVES_CACHE_MAX_BYTES")
    natives_cache_gc_interval_seconds: float = Field(3600.0, alias="ROBOMP_NATIVES_CACHE_GC_INTERVAL_SECONDS")

    # Workspace garbage collection. Bounds total workspace disk use so a
    # reconciler-driven deployment with missed close webhooks never fills the
    # disk with old PR-head worktrees. ``interval <= 0`` disables the periodic
    # sweep; ``max_age``/``max_bytes``/``min_free`` each disable only their own
    # cap when ``<= 0``.
    workspace_gc_interval_seconds: float = Field(1800.0, alias="ROBOMP_WORKSPACE_GC_INTERVAL_SECONDS")
    workspace_gc_max_age_seconds: float = Field(86400.0, alias="ROBOMP_WORKSPACE_GC_MAX_AGE_SECONDS")
    workspace_gc_max_bytes: int = Field(120 * 1024**3, alias="ROBOMP_WORKSPACE_GC_MAX_BYTES")
    workspace_gc_min_free_bytes: int = Field(100 * 1024**3, alias="ROBOMP_WORKSPACE_GC_MIN_FREE_BYTES")

    @field_validator("bot_login", mode="after")
    @classmethod
    def _require_bot_login(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("ROBOMP_BOT_LOGIN must be a non-empty GitHub login")
        return cleaned

    @field_validator("replay_token", mode="before")
    @classmethod
    def _blank_replay_disables(cls, value: object) -> object:
        # Treat empty/whitespace strings as 'disabled'. Without this, an empty
        # ROBOMP_REPLAY_TOKEN becomes SecretStr("") which the server would
        # happily compare against an empty X-Robomp-Replay-Token header.
        if isinstance(value, str) and not value.strip():
            return None
        if hasattr(value, "get_secret_value"):
            inner = value.get_secret_value()  # type: ignore[attr-defined]
            if isinstance(inner, str) and not inner.strip():
                return None
        return value

    @field_validator("pr_review_self_improve_push_token", mode="before")
    @classmethod
    def _blank_token_disables(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        if hasattr(value, "get_secret_value"):
            inner = value.get_secret_value()  # type: ignore[attr-defined]
            if isinstance(inner, str) and not inner.strip():
                return None
        return value

    @field_validator("repo_allowlist_raw", mode="before")
    @classmethod
    def _coerce_allowlist(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @field_validator("pr_review_label_allowlist_raw", mode="before")
    @classmethod
    def _coerce_pr_review_label_allowlist(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @field_validator("pr_review_delegate_models_raw", mode="before")
    @classmethod
    def _coerce_pr_review_delegate_models(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @field_validator("pr_review_delegate_model_map_raw", mode="before")
    @classmethod
    def _coerce_pr_review_delegate_model_map(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, Mapping):
            return ",".join(f"{key}={value}" for key, value in v.items())
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @field_validator("pr_review_helper", mode="before")
    @classmethod
    def _blank_pr_review_helper_disables(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("pr_review_learning_db", mode="before")
    @classmethod
    def _blank_pr_review_learning_db_disables(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def pr_review_label_allowlist(self) -> frozenset[str]:
        items = [piece.strip().lower() for piece in self.pr_review_label_allowlist_raw.split(",")]
        return frozenset(item for item in items if item)

    @property
    def pr_review_delegate_models(self) -> tuple[str, ...]:
        items = [piece.strip() for piece in self.pr_review_delegate_models_raw.split(",")]
        return tuple(item for item in items if item)

    @property
    def pr_review_delegate_model_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for piece in self.pr_review_delegate_model_map_raw.split(","):
            if "=" not in piece:
                continue
            key, value = piece.split("=", 1)
            normalized_key = key.strip().lower()
            normalized_value = value.strip()
            if normalized_key and normalized_value:
                mapping[normalized_key] = normalized_value
        return mapping

    @property
    def repo_allowlist(self) -> frozenset[str]:
        items = [piece.strip().lower() for piece in self.repo_allowlist_raw.split(",")]
        return frozenset(item for item in items if item)

    @field_validator("rate_limit_unlimited_raw", mode="before")
    @classmethod
    def _coerce_unlimited(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @property
    def rate_limit_unlimited(self) -> frozenset[str]:
        items = [piece.strip().lstrip("@").lower() for piece in self.rate_limit_unlimited_raw.split(",")]
        return frozenset(item for item in items if item)

    @field_validator("maintainer_logins_raw", mode="before")
    @classmethod
    def _coerce_maintainers(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @field_validator("reviewer_bots_raw", mode="before")
    @classmethod
    def _coerce_reviewer_bots(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @property
    def reviewer_bots(self) -> frozenset[str]:
        items = [piece.strip().lstrip("@").lower() for piece in self.reviewer_bots_raw.split(",")]
        return frozenset(item for item in items if item)

    @property
    def maintainer_logins(self) -> frozenset[str]:
        items = [piece.strip().lstrip("@").lower() for piece in self.maintainer_logins_raw.split(",")]
        return frozenset(item for item in items if item)

    def allows(self, full_name: str) -> bool:
        return full_name.lower() in self.repo_allowlist

    @property
    def model_pool(self) -> tuple[str, ...]:
        """ROBOMP_MODEL may be a single id or a comma-separated list; this
        returns the parsed pool (always non-empty)."""
        items = [piece.strip() for piece in self.model.split(",") if piece.strip()]
        return tuple(items) or (self.model,)

    def pick_model(self) -> str:
        """Random selection from the pool (uniform). One-element pools return that one."""
        return random.choice(self.model_pool)

    @property
    def resolved_author_name(self) -> str:
        """Falls back to bot_login if ROBOMP_GIT_AUTHOR_NAME isn't set."""
        return (self.git_author_name or self.bot_login).strip()

    def ensure_paths(self) -> None:
        paths = [self.workspace_root, self.sqlite_path.parent, self.log_dir]
        if self.pr_review_learning_db is not None:
            paths.append(self.pr_review_learning_db.parent)
        if self.pr_review_self_improve_enabled:
            paths.append(self.pr_review_self_improve_report_dir)
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)


class OrchestratorSettings(Settings):
    """Settings for the webhook/worker process.

    Orchestrator is proxy-only: it must not hold the GitHub PAT, and it must
    route all GitHub REST/git traffic through gh-proxy over HMAC.
    """

    github_token: SecretStr | None = Field(None, alias="GITHUB_TOKEN")
    gh_proxy_url: str = Field(..., alias="ROBOMP_GH_PROXY_URL")
    gh_proxy_hmac_key: SecretStr = Field(..., alias="ROBOMP_GH_PROXY_HMAC_KEY")

    @field_validator("github_token", mode="before")
    @classmethod
    def _blank_github_token_disables(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        if hasattr(value, "get_secret_value"):
            inner = value.get_secret_value()  # type: ignore[attr-defined]
            if isinstance(inner, str) and not inner.strip():
                return None
        return value

    @field_validator("gh_proxy_url", mode="before")
    @classmethod
    def _require_proxy_url(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("ROBOMP_GH_PROXY_URL must be set for orchestrator mode")
        return value

    @field_validator("gh_proxy_hmac_key", mode="before")
    @classmethod
    def _require_proxy_key(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("ROBOMP_GH_PROXY_HMAC_KEY must be set for orchestrator mode")
        if hasattr(value, "get_secret_value"):
            inner = value.get_secret_value()  # type: ignore[attr-defined]
            if isinstance(inner, str) and not inner.strip():
                raise ValueError("ROBOMP_GH_PROXY_HMAC_KEY must be set for orchestrator mode")
        return value

    @model_validator(mode="after")
    def _validate_proxy_only(self) -> OrchestratorSettings:
        if self.github_token is not None:
            raise ValueError(
                "robomp orchestrator refuses to start with GITHUB_TOKEN set in env. "
                "The PAT must live only in the gh-proxy container."
            )
        return self


@cache
def get_settings() -> OrchestratorSettings:
    return OrchestratorSettings()  # type: ignore[call-arg]


def reset_settings_cache() -> None:
    """Invalidate the cached settings (tests)."""
    get_settings.cache_clear()


class ProxySettings(BaseSettings):
    """Settings for the gh-proxy process only."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    github_token: SecretStr = Field(..., alias="GITHUB_TOKEN")
    gh_proxy_hmac_key: SecretStr = Field(..., alias="ROBOMP_GH_PROXY_HMAC_KEY")
    gh_proxy_bind_host: str = Field("0.0.0.0", alias="ROBOMP_GH_PROXY_BIND_HOST")
    gh_proxy_bind_port: int = Field(8081, alias="ROBOMP_GH_PROXY_BIND_PORT")
    workspace_root: Path = Field(Path("./data/workspaces"), alias="ROBOMP_WORKSPACE_ROOT")
    log_dir: Path = Field(Path("./data/logs"), alias="ROBOMP_LOG_DIR")
    gh_proxy_max_body_bytes: int = Field(1 << 20, alias="ROBOMP_GH_PROXY_MAX_BODY_BYTES")
    gh_proxy_git_timeout_seconds: float = Field(60.0, alias="ROBOMP_GH_PROXY_GIT_TIMEOUT_SECONDS")

    @field_validator("github_token", "gh_proxy_hmac_key", mode="before")
    @classmethod
    def _reject_blank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("must be a non-empty string")
        if hasattr(value, "get_secret_value"):
            inner = value.get_secret_value()  # type: ignore[attr-defined]
            if isinstance(inner, str) and not inner.strip():
                raise ValueError("must be a non-empty string")
        return value

    def ensure_paths(self) -> None:
        for path in (self.workspace_root, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_proxy_settings() -> ProxySettings:
    """Build settings for the gh-proxy process."""
    return ProxySettings()  # type: ignore[call-arg]
