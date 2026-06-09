from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from robomp.config import (
    OrchestratorSettings as Settings,
)
from robomp.config import (
    ProxySettings,
    load_proxy_settings,
    reset_settings_cache,
)


def test_settings_load_from_env(env: dict[str, str]) -> None:
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.bot_login == "robomp-bot"
    assert cfg.repo_allowlist == frozenset({"octo/widget"})
    assert cfg.allows("octo/widget")
    assert cfg.allows("Octo/Widget")
    assert not cfg.allows("other/widget")


def test_settings_missing_required(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    """Empty out every credential source: validator MUST trip the
    'no GitHub access configured' branch. The `env` fixture keeps the other
    required fields satisfied so we isolate the credential-validator path."""
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("ROBOMP_GH_PROXY_URL", "")
    monkeypatch.setenv("ROBOMP_GH_PROXY_HMAC_KEY", "")
    reset_settings_cache()
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_orchestrator_mode_loads_proxy_config(env: dict[str, str]) -> None:
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.github_token is None
    assert cfg.gh_proxy_url == "http://gh-proxy.invalid:8081"
    assert cfg.gh_proxy_hmac_key is not None
    assert cfg.gh_proxy_hmac_key.get_secret_value().startswith("test-hmac-key")
    assert cfg.labels_comments_only is False
    assert cfg.pr_review_label_allowlist == frozenset()
    assert cfg.pr_review_terminal_events is False

    assert cfg.pr_review_delegate_models == ()

def test_labels_comments_only_env_parses(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_LABELS_COMMENTS_ONLY", "true")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.labels_comments_only is True


def test_rejects_token_and_proxy_together(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    reset_settings_cache()
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_rejects_proxy_url_without_key(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_GH_PROXY_HMAC_KEY", "")
    reset_settings_cache()
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_proxy_mode_loads_pat(proxy_env: dict[str, str]) -> None:
    cfg = load_proxy_settings()
    assert isinstance(cfg, ProxySettings)
    assert cfg.github_token.get_secret_value() == "ghp_test_token_value_xxxxxxxxxxxxxxxx"
    assert cfg.gh_proxy_hmac_key.get_secret_value().startswith("test-hmac-key")


def test_allowlist_csv_parsing(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_REPO_ALLOWLIST", "  alpha/one ,beta/two, ,gamma/three ")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.repo_allowlist == frozenset({"alpha/one", "beta/two", "gamma/three"})

def test_pr_review_label_allowlist_parses(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_PR_REVIEW_LABEL_ALLOWLIST", "robo-review")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.pr_review_label_allowlist == frozenset({"robo-review"})


def test_pr_review_delegate_models_parse(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_PR_REVIEW_DELEGATE_MODELS", "google-gemini-cli/gemini-3.1-pro-preview, anthropic/claude-opus-4-8")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.pr_review_delegate_models == (
        "google-gemini-cli/gemini-3.1-pro-preview",
        "anthropic/claude-opus-4-8",
    )


def test_pr_review_delegate_model_map_parse(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv(
        "ROBOMP_PR_REVIEW_DELEGATE_MODEL_MAP",
        "default=openai-codex/gpt-5.5, security=anthropic/claude-opus-4-8",
    )
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.pr_review_delegate_model_map == {
        "default": "openai-codex/gpt-5.5",
        "security": "anthropic/claude-opus-4-8",
    }


def test_pr_review_helper_blank_or_path(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_PR_REVIEW_HELPER", "   ")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.pr_review_helper is None

    monkeypatch.setenv("ROBOMP_PR_REVIEW_HELPER", "/opt/pr-review-kit/Tools/pr-review-evidence.ts")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert str(cfg.pr_review_helper) == "/opt/pr-review-kit/Tools/pr-review-evidence.ts"

def test_pr_review_learning_db_blank_or_path(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_PR_REVIEW_LEARNING_DB", "   ")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.pr_review_learning_db is None

    monkeypatch.setenv("ROBOMP_PR_REVIEW_LEARNING_DB", "/data/pr-review/review-learning.sqlite")
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.pr_review_learning_db == Path("/data/pr-review/review-learning.sqlite")

def test_pr_review_self_improvement_defaults(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.delenv("ROBOMP_PR_REVIEW_SELF_IMPROVE_REPORT_DIR", raising=False)
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.pr_review_self_improve_enabled is True
    assert cfg.pr_review_self_improve_batch_size == 10
    assert cfg.pr_review_self_improve_lookback_days == 30
    assert cfg.pr_review_self_improve_max_sessions == 50
    assert cfg.pr_review_self_improve_report_dir == Path("/data/pr-review/self-improvements")
    assert cfg.pr_review_self_improve_repo_root == Path("/source/robo-ms")
    assert cfg.pr_review_self_improve_model == ""
    assert cfg.pr_review_self_improve_quality_gate_timeout_seconds == 3600.0
    assert cfg.pr_review_self_improve_push_token is None


def test_pr_review_self_improvement_push_token(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_PR_REVIEW_SELF_IMPROVE_PUSH_TOKEN", "secret-token")
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.pr_review_self_improve_push_token is not None
    assert cfg.pr_review_self_improve_push_token.get_secret_value() == "secret-token"


def test_ensure_paths_creates_pr_review_learning_dirs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env: dict[str, str],
) -> None:
    learning_db = tmp_path / "learning" / "review-learning.sqlite"
    report_dir = tmp_path / "reports"
    monkeypatch.setenv("ROBOMP_PR_REVIEW_LEARNING_DB", str(learning_db))
    monkeypatch.setenv("ROBOMP_PR_REVIEW_SELF_IMPROVE_REPORT_DIR", str(report_dir))
    cfg = Settings()  # type: ignore[call-arg]

    cfg.ensure_paths()

    assert learning_db.parent.is_dir()
    assert report_dir.is_dir()


def test_blank_replay_token_treated_as_disabled(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_REPLAY_TOKEN", "")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.replay_token is None


def test_whitespace_replay_token_treated_as_disabled(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_REPLAY_TOKEN", "   ")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.replay_token is None


def test_real_replay_token_preserved(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_REPLAY_TOKEN", "abc")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.replay_token is not None
    assert cfg.replay_token.get_secret_value() == "abc"


def test_blank_bot_login_rejected(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_BOT_LOGIN", "   ")
    reset_settings_cache()
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_model_pool_single(env: dict[str, str]) -> None:
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.model_pool == (cfg.model,)
    assert cfg.pick_model() == cfg.model


def test_model_pool_csv_parses(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv(
        "ROBOMP_MODEL",
        " codex/gpt-5.4 , anthropic/claude-sonnet-4-6 ,, anthropic/claude-opus-4-7 ",
    )
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.model_pool == (
        "codex/gpt-5.4",
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-opus-4-7",
    )


def test_pick_model_covers_full_pool(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    """With a 3-item pool and 500 picks, each option appears at least once."""
    monkeypatch.setenv("ROBOMP_MODEL", "a,b,c")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    seen = {cfg.pick_model() for _ in range(500)}
    assert seen == {"a", "b", "c"}


def test_max_concurrency_default_is_8(env: dict[str, str]) -> None:
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.max_concurrency == 8


def test_task_timeout_hard_grace_env_parses(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_TASK_TIMEOUT_HARD_GRACE_SECONDS", "12.5")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.task_timeout_hard_grace_seconds == 12.5
