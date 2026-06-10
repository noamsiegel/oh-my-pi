"""HMAC + endpoint coverage for the gh-proxy FastAPI app."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from robomp.config import ProxySettings
from robomp.github_client import GitHubClient
from robomp.proxy.server import create_proxy_app
from robomp.proxy_hmac import HEADER_SIGNATURE, HEADER_TIMESTAMP, sign
from robomp.sandbox import workspace_key

_HMAC = "test-hmac-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_TOKEN = "ghp_test_token_value"


# ---------- shared fixtures ----------


def _build_settings(tmp_path: Path) -> ProxySettings:
    """Construct a ProxySettings object for the proxy side."""
    cfg = ProxySettings.model_construct(
        github_token=SecretStr(_TOKEN),
        gh_proxy_hmac_key=SecretStr(_HMAC),
        gh_proxy_bind_host="0.0.0.0",
        gh_proxy_bind_port=8081,
        workspace_root=tmp_path / "workspaces",
        log_dir=tmp_path / "logs",
        gh_proxy_max_body_bytes=1 << 20,
        gh_proxy_git_timeout_seconds=60.0,
    )
    cfg.ensure_paths()
    return cfg


@pytest.fixture
def proxy_settings(tmp_path: Path) -> ProxySettings:
    return _build_settings(tmp_path)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Path:
    """Bare local repo with one commit on `main`."""
    repo = tmp_path / "upstream.git"
    repo.mkdir()
    _git(["init", "--initial-branch=main", "--bare", str(repo)], tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "--initial-branch=main", str(seed)], tmp_path)
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["-C", str(seed), "add", "."], tmp_path)
    _git(["-C", str(seed), "commit", "-m", "init"], tmp_path)
    _git(["-C", str(seed), "remote", "add", "origin", str(repo)], tmp_path)
    _git(["-C", str(seed), "push", "origin", "main"], tmp_path)
    return repo

def _partial_clone_upstream(tmp_path: Path) -> Path:
    repo = tmp_path / "partial-upstream.git"
    repo.mkdir()
    _git(["init", "--initial-branch=main", "--bare", str(repo)], tmp_path)
    _git(["-C", str(repo), "config", "uploadpack.allowFilter", "true"], tmp_path)
    _git(["-C", str(repo), "config", "uploadpack.allowAnySHA1InWant", "true"], tmp_path)
    seed = tmp_path / "partial-seed"
    seed.mkdir()
    _git(["init", "--initial-branch=main", str(seed)], tmp_path)
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["-C", str(seed), "add", "."], tmp_path)
    _git(["-C", str(seed), "commit", "-m", "init"], tmp_path)
    _git(["-C", str(seed), "remote", "add", "origin", str(repo)], tmp_path)
    _git(["-C", str(seed), "push", "origin", "main"], tmp_path)
    return repo


def _publish_pr_with_files(upstream: Path, tmp_path: Path) -> str:
    pr_seed = tmp_path / "proxy-pr-seed"
    _git(["clone", f"file://{upstream}", str(pr_seed)], tmp_path)
    (pr_seed / "src").mkdir()
    (pr_seed / "docs").mkdir()
    (pr_seed / "src" / "changed.txt").write_text("changed payload\n", encoding="utf-8")
    (pr_seed / "docs" / "untouched.txt").write_text("untouched payload\n", encoding="utf-8")
    _git(["-C", str(pr_seed), "add", "src/changed.txt", "docs/untouched.txt"], tmp_path)
    _git(["-C", str(pr_seed), "commit", "-m", "add pr files"], tmp_path)
    pr_sha = subprocess.run(["git", "-C", str(pr_seed), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    _git(["-C", str(pr_seed), "push", "origin", "HEAD:refs/pull/7/head"], tmp_path)
    return pr_sha


def _partial_pool(proxy_settings: ProxySettings, upstream: Path, tmp_path: Path) -> Path:
    pool = Path(proxy_settings.workspace_root) / "_pool" / "test__widget"
    pool.parent.mkdir(parents=True, exist_ok=True)
    _git(
        [
            "clone",
            "--filter=blob:none",
            "--no-tags",
            "--branch",
            "main",
            f"file://{upstream}",
            str(pool),
        ],
        tmp_path,
    )
    return pool


def _stage_workspace(cfg: ProxySettings, upstream: Path, repo: str, number: int, branch: str) -> tuple[Path, str]:
    """Pre-stage a workspace clone with one new commit on `branch`."""
    ws_dir = Path(cfg.workspace_root) / workspace_key(repo, number)
    ws_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = ws_dir / "repo"
    _git(["clone", str(upstream), str(repo_dir)], ws_dir)
    _git(["-C", str(repo_dir), "config", "user.email", "t@t"], ws_dir)
    _git(["-C", str(repo_dir), "config", "user.name", "t"], ws_dir)
    _git(["-C", str(repo_dir), "checkout", "-b", branch], ws_dir)
    (repo_dir / "x.txt").write_text("x", encoding="utf-8")
    _git(["-C", str(repo_dir), "add", "."], ws_dir)
    _git(["-C", str(repo_dir), "commit", "-m", "x"], ws_dir)
    proc = _git(["-C", str(repo_dir), "rev-parse", "HEAD"], ws_dir)
    return repo_dir, proc.stdout.strip()


def _bare_has_branch(bare: Path, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(bare), "branch", "--list", branch],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())


# ---------- HMAC + signed request helpers ----------


def _signed(
    method: str,
    path: str,
    body: bytes = b"",
    *,
    params: dict[str, object] | None = None,
    ts: str | None = None,
    key: bytes | None = None,
) -> dict[str, str]:
    """Build signed headers.

    When `params` is supplied, the canonical signing target becomes
    `path?<query>` (matching the verifier's request-target reconstruction),
    so signed requests with query strings stay verifiable AND mutating any
    query parameter post-sign produces a 401.
    """
    if params:
        url = httpx.URL(path, params=params)
        query = url.query.decode("ascii") if url.query else ""
        target = f"{path}?{query}" if query else path
    else:
        target = path
    timestamp, sig = sign(method=method, path=target, body=body, key=key or _HMAC.encode(), timestamp=ts)
    return {HEADER_TIMESTAMP: timestamp, HEADER_SIGNATURE: sig}


def _build_app(cfg: ProxySettings, gh_handler: Callable[[httpx.Request], httpx.Response] | None = None):
    app = create_proxy_app(cfg)
    transport = httpx.MockTransport(gh_handler) if gh_handler is not None else None
    app.state.github = GitHubClient(_TOKEN, transport=transport)
    app.state.settings = cfg
    return app


async def _async_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    )


def test_read_origin_url_uses_safe_directory_and_slot_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from robomp.proxy import server as proxy_server

    captured: dict[str, object] = {}
    repo_dir = tmp_path / "repo"

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "https://github.com/octo/widget.git\n", "")

    monkeypatch.setattr("robomp.proxy.server.subprocess.run", fake_run)
    monkeypatch.setattr(
        "robomp.proxy.server._slot_subprocess_kwargs",
        lambda uid: {"user": uid, "group": uid, "extra_groups": [2000], "umask": 0o002},
    )

    assert proxy_server._read_origin_url(repo_dir, slot_uid=2001) == "https://github.com/octo/widget.git"

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_0"] == str(repo_dir)
    assert captured["user"] == 2001
    assert captured["group"] == 2001
    assert captured["extra_groups"] == [2000]
    assert captured["umask"] == 0o002


# ============================================================================
# HMAC behavior
# ============================================================================


async def test_hmac_accept_post_comment_round_trip(proxy_settings: ProxySettings) -> None:
    captured: dict[str, httpx.Request] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return httpx.Response(
            201,
            json={
                "id": 42,
                "user": {"login": "robomp-bot"},
                "body": "hello",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )

    app = _build_app(proxy_settings, gh)
    body = b'{"repo":"octo/widget","number":1,"body":"hello"}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/post_comment",
            content=body,
            headers={**_signed("POST", "/gh/v1/post_comment", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"id": 42, "author": "robomp-bot", "body": "hello", "created_at": "2026-01-01T00:00:00Z"}
    assert captured["req"].url.path == "/repos/octo/widget/issues/1/comments"


async def test_hmac_reject_missing_headers(proxy_settings: ProxySettings) -> None:
    app = _build_app(proxy_settings, lambda _: httpx.Response(200, json={}))
    async with await _async_client(app) as client:
        resp = await client.get("/gh/v1/repo", params={"repo": "octo/widget"})
    assert resp.status_code == 401


async def test_hmac_reject_bad_signature(proxy_settings: ProxySettings) -> None:
    app = _build_app(proxy_settings, lambda _: httpx.Response(200, json={}))
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/repo",
            params={"repo": "octo/widget"},
            headers={HEADER_TIMESTAMP: str(int(time.time())), HEADER_SIGNATURE: "0" * 64},
        )
    assert resp.status_code == 401


async def test_hmac_reject_stale_timestamp(proxy_settings: ProxySettings) -> None:
    app = _build_app(proxy_settings, lambda _: httpx.Response(200, json={}))
    stale = str(int(time.time()) - 120)
    headers = _signed("GET", "/gh/v1/repo", ts=stale)
    async with await _async_client(app) as client:
        resp = await client.get("/gh/v1/repo", params={"repo": "octo/widget"}, headers=headers)
    assert resp.status_code == 401


# ============================================================================
# GET endpoints
# ============================================================================


async def test_get_repo(proxy_settings: ProxySettings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/octo/widget"
        return httpx.Response(
            200,
            json={
                "full_name": "octo/widget",
                "default_branch": "main",
                "clone_url": "https://github.com/octo/widget.git",
                "private": False,
            },
        )

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/repo",
            params={"repo": "octo/widget"},
            headers=_signed("GET", "/gh/v1/repo", params={"repo": "octo/widget"}),
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "full_name": "octo/widget",
        "default_branch": "main",
        "clone_url": "https://github.com/octo/widget.git",
        "private": False,
    }


async def test_get_issue(proxy_settings: ProxySettings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/octo/widget/issues/1"
        return httpx.Response(
            200,
            json={
                "number": 1,
                "title": "T",
                "body": "B",
                "state": "open",
                "user": {"login": "alice"},
                "labels": [{"name": "bug"}],
            },
        )

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/issue",
            params={"repo": "octo/widget", "number": 1},
            headers=_signed("GET", "/gh/v1/issue", params={"repo": "octo/widget", "number": 1}),
        )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["repo"] == "octo/widget"
    assert payload["number"] == 1
    assert payload["labels"] == ["bug"]
    assert payload["is_pull_request"] is False


async def test_list_issues(proxy_settings: ProxySettings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/octo/widget/issues"
        return httpx.Response(
            200,
            json=[
                {
                    "number": 1,
                    "title": "first",
                    "state": "open",
                    "user": {"login": "alice"},
                    "labels": [{"name": "bug"}],
                    "comments": 0,
                    "updated_at": "2026-01-01T00:00:00Z",
                    "created_at": "2026-01-01T00:00:00Z",
                    "html_url": "https://example/1",
                },
                # A PR — must be filtered out.
                {
                    "number": 2,
                    "title": "pr",
                    "pull_request": {"url": "x"},
                    "user": {"login": "alice"},
                },
            ],
        )

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/issues",
            params={"repo": "octo/widget"},
            headers=_signed("GET", "/gh/v1/issues", params={"repo": "octo/widget"}),
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["number"] == 1


async def test_list_comments(proxy_settings: ProxySettings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/octo/widget/issues/1/comments"
        return httpx.Response(
            200,
            json=[
                {"id": 1, "user": {"login": "u"}, "body": "hi", "created_at": "2026-01-01T00:00:00Z"},
            ],
        )

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/comments",
            params={"repo": "octo/widget", "number": 1},
            headers=_signed("GET", "/gh/v1/comments", params={"repo": "octo/widget", "number": 1}),
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "items": [{"id": 1, "author": "u", "body": "hi", "created_at": "2026-01-01T00:00:00Z"}],
    }


async def test_list_review_comments(proxy_settings: ProxySettings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/octo/widget/pulls/1/comments"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 9,
                    "user": {"login": "rev"},
                    "body": "nit",
                    "path": "a.py",
                    "line": 5,
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
        )

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/review_comments",
            params={"repo": "octo/widget", "pr_number": 1},
            headers=_signed("GET", "/gh/v1/review_comments", params={"repo": "octo/widget", "pr_number": 1}),
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["path"] == "a.py"
    assert items[0]["line"] == 5



async def test_list_review_threads(proxy_settings: ProxySettings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/graphql"
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "thread-1",
                                        "isResolved": True,
                                        "isOutdated": False,
                                        "resolvedBy": {"login": "maintainer"},
                                        "comments": {
                                            "nodes": [
                                                {
                                                    "databaseId": 9,
                                                    "author": {"login": "rev"},
                                                    "body": "nit",
                                                    "path": "a.py",
                                                    "line": 5,
                                                    "originalLine": 5,
                                                    "startLine": None,
                                                    "originalStartLine": None,
                                                    "url": "u",
                                                    "diffHunk": "@@",
                                                    "outdated": False,
                                                    "state": "SUBMITTED",
                                                    "replyTo": None,
                                                    "commit": {"oid": "abc"},
                                                    "pullRequestReview": {"databaseId": 44},
                                                    "createdAt": "2026-01-01T00:00:00Z",
                                                }
                                            ]
                                        },
                                    }
                                ],
                            }
                        }
                    }
                }
            },
        )

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/review_threads",
            params={"repo": "octo/widget", "pr_number": 1},
            headers=_signed("GET", "/gh/v1/review_threads", params={"repo": "octo/widget", "pr_number": 1}),
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["is_resolved"] is True
    assert items[0]["resolved_by"] == "maintainer"
    assert items[0]["comments"][0]["review_id"] == 44


async def test_list_review_comments_for_review(proxy_settings: ProxySettings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/octo/widget/pulls/1/reviews/44/comments"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 55,
                    "pull_request_review_id": 44,
                    "user": {"login": "rev"},
                    "body": "nit",
                    "path": "a.py",
                    "line": 5,
                    "created_at": "2026-01-01T00:00:00Z",
                    "commit_id": "abc",
                    "diff_hunk": "@@ -1 +1 @@",
                    "in_reply_to_id": 54,
                }
            ],
        )

    app = _build_app(proxy_settings, gh)
    params = {"repo": "octo/widget", "pr_number": 1, "review_id": 44}
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/review_comments_for_review",
            params=params,
            headers=_signed("GET", "/gh/v1/review_comments_for_review", params=params),
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["review_id"] == 44
    assert items[0]["commit_id"] == "abc"
    assert items[0]["in_reply_to_id"] == 54


async def test_list_pr_reviews(proxy_settings: ProxySettings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/octo/widget/pulls/1/reviews"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 11,
                    "user": {"login": "rev"},
                    "body": "looks good",
                    "state": "APPROVED",
                    "submitted_at": "2026-01-01T00:00:00Z",
                    "commit_id": "abc",
                },
                # Empty body — must be filtered out by GitHubClient.
                {"id": 12, "user": {"login": "rev"}, "body": "  ", "state": "COMMENTED"},
            ],
        )

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/pr_reviews",
            params={"repo": "octo/widget", "pr_number": 1},
            headers=_signed("GET", "/gh/v1/pr_reviews", params={"repo": "octo/widget", "pr_number": 1}),
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["state"] == "APPROVED"
    assert items[0]["commit_id"] == "abc"


async def test_list_pr_commits(proxy_settings: ProxySettings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/octo/widget/pulls/1/commits"
        return httpx.Response(
            200,
            json=[
                {
                    "sha": "abc",
                    "author": {"login": "alice"},
                    "commit": {"message": "Fix\nbody", "author": {"name": "Alice"}},
                }
            ],
        )

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/pr_commits",
            params={"repo": "octo/widget", "pr_number": 1},
            headers=_signed("GET", "/gh/v1/pr_commits", params={"repo": "octo/widget", "pr_number": 1}),
        )
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["sha"] == "abc"
    assert item["authors"][0]["login"] == "alice"


async def test_get_commit_ci_status(proxy_settings: ProxySettings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/octo/widget/commits/abc123/check-runs":
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "name": "build",
                            "status": "completed",
                            "conclusion": "success",
                            "details_url": "https://ci/build",
                        }
                    ]
                },
            )
        if req.url.path == "/repos/octo/widget/commits/abc123/status":
            return httpx.Response(200, json={"statuses": []})
        return httpx.Response(404, json={"message": req.url.path})

    params = {"repo": "octo/widget", "head_sha": "abc123"}
    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/commit_ci_status",
            params=params,
            headers=_signed("GET", "/gh/v1/commit_ci_status", params=params),
        )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["head_sha"] == "abc123"
    assert payload["state"] == "passed"
    assert payload["checks"][0]["name"] == "build"


async def test_authenticated_login(proxy_settings: ProxySettings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/user"
        return httpx.Response(200, json={"login": "robomp-bot"})

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/authenticated_login",
            headers=_signed("GET", "/gh/v1/authenticated_login"),
        )
    assert resp.status_code == 200
    assert resp.json() == {"login": "robomp-bot"}


# ============================================================================
# POST endpoints
# ============================================================================


async def test_post_comment_forwards_body(proxy_settings: ProxySettings) -> None:
    captured: dict[str, httpx.Request] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return httpx.Response(
            201,
            json={"id": 7, "user": {"login": "b"}, "body": "hi", "created_at": "2026-01-01T00:00:00Z"},
        )

    app = _build_app(proxy_settings, gh)
    body = b'{"repo":"octo/widget","number":1,"body":"hi"}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/post_comment",
            content=body,
            headers={**_signed("POST", "/gh/v1/post_comment", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    req = captured["req"]
    assert req.method == "POST"
    assert req.url.path == "/repos/octo/widget/issues/1/comments"
    import json

    assert json.loads(req.content) == {"body": "hi"}


async def test_add_issue_labels(proxy_settings: ProxySettings) -> None:
    captured: dict[str, httpx.Request] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return httpx.Response(200, json=[{"name": "triage"}, {"name": "bug"}])

    app = _build_app(proxy_settings, gh)
    body = b'{"repo":"octo/widget","number":1,"labels":["triage","bug"]}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/add_issue_labels",
            content=body,
            headers={**_signed("POST", "/gh/v1/add_issue_labels", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"labels": ["triage", "bug"]}
    assert captured["req"].url.path == "/repos/octo/widget/issues/1/labels"
    import json

    assert json.loads(captured["req"].content) == {"labels": ["triage", "bug"]}


async def test_add_assignees(proxy_settings: ProxySettings) -> None:
    captured: dict[str, httpx.Request] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return httpx.Response(201, json={})

    app = _build_app(proxy_settings, gh)
    body = b'{"repo":"octo/widget","number":1,"assignees":["alice"]}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/add_assignees",
            content=body,
            headers={**_signed("POST", "/gh/v1/add_assignees", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert captured["req"].url.path == "/repos/octo/widget/issues/1/assignees"
    import json

    assert json.loads(captured["req"].content) == {"assignees": ["alice"]}


async def test_comment_reactions(proxy_settings: ProxySettings) -> None:
    captured: dict[str, httpx.Request] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return httpx.Response(
            200,
            json=[
                {"content": "-1", "user": {"login": "alice", "type": "User"}},
            ],
        )

    app = _build_app(proxy_settings, gh)
    target = "/gh/v1/comment_reactions?repo=octo%2Fwidget&comment_id=999"
    async with await _async_client(app) as client:
        resp = await client.get(target, headers=_signed("GET", target))
    assert resp.status_code == 200
    assert resp.json() == {
        "items": [{"content": "-1", "user_login": "alice", "user_type": "User"}],
    }
    req = captured["req"]
    assert req.method == "GET"
    assert req.url.path == "/repos/octo/widget/issues/comments/999/reactions"
    assert req.url.params.get("content") == "-1"


async def test_close_issue(proxy_settings: ProxySettings) -> None:
    captured: dict[str, httpx.Request] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return httpx.Response(200, json={})

    app = _build_app(proxy_settings, gh)
    body = b'{"repo":"octo/widget","number":7,"reason":"completed"}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/close_issue",
            content=body,
            headers={**_signed("POST", "/gh/v1/close_issue", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    req = captured["req"]
    assert req.method == "PATCH"
    assert req.url.path == "/repos/octo/widget/issues/7"
    import json

    assert json.loads(req.content) == {"state": "closed", "state_reason": "completed"}


async def test_close_issue_defaults_reason_to_completed(proxy_settings: ProxySettings) -> None:
    captured: dict[str, httpx.Request] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return httpx.Response(200, json={})

    app = _build_app(proxy_settings, gh)
    body = b'{"repo":"octo/widget","number":7}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/close_issue",
            content=body,
            headers={**_signed("POST", "/gh/v1/close_issue", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    import json

    assert json.loads(captured["req"].content) == {"state": "closed", "state_reason": "completed"}


async def test_open_pull_request(proxy_settings: ProxySettings) -> None:
    captured: dict[str, httpx.Request] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return httpx.Response(
            201,
            json={
                "number": 4,
                "html_url": "https://example/4",
                "head": {"ref": "feature"},
                "base": {"ref": "main"},
                "state": "open",
            },
        )

    app = _build_app(proxy_settings, gh)
    body = (
        b'{"repo":"octo/widget","head":"feature","base":"main",'
        b'"title":"t","body":"b","draft":false,"maintainer_can_modify":true}'
    )
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/open_pull_request",
            content=body,
            headers={**_signed("POST", "/gh/v1/open_pull_request", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert resp.json()["number"] == 4
    assert captured["req"].url.path == "/repos/octo/widget/pulls"
    import json

    sent = json.loads(captured["req"].content)
    assert sent["head"] == "feature"
    assert sent["base"] == "main"
    assert sent["title"] == "t"


async def test_request_reviewers(proxy_settings: ProxySettings) -> None:
    captured: dict[str, httpx.Request] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return httpx.Response(201, json={})

    app = _build_app(proxy_settings, gh)
    body = b'{"repo":"octo/widget","pr_number":4,"reviewers":["alice"],"team_reviewers":null}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/request_reviewers",
            content=body,
            headers={**_signed("POST", "/gh/v1/request_reviewers", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert captured["req"].url.path == "/repos/octo/widget/pulls/4/requested_reviewers"
    import json

    assert json.loads(captured["req"].content) == {"reviewers": ["alice"]}


# ============================================================================
# GitHub error passthrough
# ============================================================================


async def test_github_error_passthrough_422(proxy_settings: ProxySettings) -> None:
    def gh(_: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "validation failed"})

    app = _build_app(proxy_settings, gh)
    body = b'{"repo":"octo/widget","number":1,"body":"hi"}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/post_comment",
            content=body,
            headers={**_signed("POST", "/gh/v1/post_comment", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["kind"] == "github"
    assert err["status"] == 422
    assert err["message"] == "validation failed"


# ============================================================================
# git transport endpoints
# ============================================================================


async def test_git_clone_creates_pool_dir(proxy_settings: ProxySettings, upstream_repo: Path) -> None:
    app = _build_app(proxy_settings)
    body = b'{"repo":"test/widget","clone_url":"' + str(upstream_repo).encode() + b'","default_branch":"main"}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/clone",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/clone", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    pool_dir = Path(resp.json()["pool_dir"])
    assert pool_dir.is_dir()
    assert pool_dir == Path(proxy_settings.workspace_root) / "_pool" / "test__widget"
    assert (pool_dir / "HEAD").exists() or (pool_dir / ".git" / "HEAD").exists()


async def test_git_clone_rejects_local_remote_for_non_test_repo(proxy_settings: ProxySettings, upstream_repo: Path) -> None:
    app = _build_app(proxy_settings)
    body = b'{"repo":"octo/widget","clone_url":"' + str(upstream_repo).encode() + b'","default_branch":"main"}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/clone",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/clone", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    assert "local git remotes" in resp.text


async def test_git_clone_rejects_mismatched_repo_url(proxy_settings: ProxySettings) -> None:
    app = _build_app(proxy_settings)
    body = b'{"repo":"octo/widget","clone_url":"https://github.com/attacker/other.git","default_branch":"main"}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/clone",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/clone", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    assert "clone url does not match repo" in resp.text


def test_git_error_response_redacts_authorization() -> None:
    test_git_error_response_redacts_secrets()




def test_git_error_response_redacts_secrets() -> None:
    from robomp.git_ops import GitCommandError
    from robomp.proxy import server as proxy_server

    token = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    exc = GitCommandError(
        ["git", "fetch", f"https://x-access-token:{token}@github.com/octo/widget.git"],
        128,
        f"Authorization: Bearer {token}\n",
        f"remote: https://alice:{token}@github.com/octo/widget.git\n",
    )
    resp = proxy_server._git_error_response(exc)  # noqa: SLF001 - redaction boundary
    payload = json.loads(resp.body)
    encoded = json.dumps(payload)
    assert token not in encoded
    assert "Authorization: Bearer ***" in encoded
    assert "https://***@github.com/octo/widget.git" in encoded

async def test_git_fetch_repairs_missing_alternate_and_bad_ref(proxy_settings: ProxySettings, upstream_repo: Path) -> None:
    pool_dir = Path(proxy_settings.workspace_root) / "_pool" / "test__widget"
    pool_dir.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", "--filter=blob:none", str(upstream_repo), str(pool_dir)], Path(proxy_settings.workspace_root))

    bad_ref = pool_dir / ".git" / "refs" / "heads" / "farm" / "bad"
    bad_ref.parent.mkdir(parents=True, exist_ok=True)
    bad_ref.write_text("0123456789012345678901234567890123456789\n", encoding="ascii")

    alternates = pool_dir / ".git" / "objects" / "info" / "alternates"
    alternates.write_text(str(Path(proxy_settings.workspace_root) / "missing-objects") + "\n", encoding="utf-8")

    app = _build_app(proxy_settings)
    body = b'{"repo":"test/widget"}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/fetch",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/fetch", body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 200, resp.text
    assert Path(resp.json()["pool_dir"]) == pool_dir
    assert not bad_ref.exists()
    assert not alternates.exists()


async def test_git_prepare_pr_worktree_sparse_endpoint(proxy_settings: ProxySettings, tmp_path: Path) -> None:
    upstream = _partial_clone_upstream(tmp_path)
    pr_sha = _publish_pr_with_files(upstream, tmp_path)
    _partial_pool(proxy_settings, upstream, tmp_path)
    app = _build_app(proxy_settings)
    body = (
        b'{"repo":"test/widget","workspace_key":"test__widget__7__'
        + pr_sha.encode()
        + b'","pr_number":7,"expected_head_sha":"'
        + pr_sha.encode()
        + b'","base_ref":"main","changed_paths":["src/changed.txt"]}'
    )
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/prepare_pr_worktree",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/prepare_pr_worktree", body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["head"] == pr_sha
    repo_dir = Path(data["repo_dir"])
    assert repo_dir.exists()
    assert data["hydrated_paths"] == ["src/changed.txt"]
    assert (repo_dir / "src/changed.txt").read_text(encoding="utf-8") == "changed payload\n"
    assert not (repo_dir / "docs" / "untouched.txt").exists()


async def test_git_prepare_pr_worktree_rejects_bad_workspace_key(
    proxy_settings: ProxySettings, tmp_path: Path
) -> None:
    upstream = _partial_clone_upstream(tmp_path)
    pr_sha = _publish_pr_with_files(upstream, tmp_path)
    _partial_pool(proxy_settings, upstream, tmp_path)
    app = _build_app(proxy_settings)
    body = (
        b'{"repo":"test/widget","workspace_key":"other__repo__7","pr_number":7,'
        b'"expected_head_sha":"'
        + pr_sha.encode()
        + b'","base_ref":"main","changed_paths":["src/changed.txt"]}'
    )
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/prepare_pr_worktree",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/prepare_pr_worktree", body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 400
    assert not (Path(proxy_settings.workspace_root) / "other__repo__7" / "repo").exists()


async def test_git_prepare_pr_worktree_uses_proxy_timeout_setting(
    proxy_settings: ProxySettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from robomp.proxy import server as proxy_server

    captured: dict[str, object] = {}
    proxy_settings.gh_proxy_git_timeout_seconds = 42.0

    def fake_prepare_pr_worktree(
        pool: Path,
        repo_dir: Path,
        *,
        pr_number: int,
        expected_head_sha: str,
        base_ref: str,
        changed_paths: list[str],
        token: str | None,
        timeout: float | None = None,
    ):
        captured["pool"] = pool
        captured["repo_dir"] = repo_dir
        captured["pr_number"] = pr_number
        captured["base_ref"] = base_ref
        captured["changed_paths"] = changed_paths
        captured["expected_head_sha"] = expected_head_sha
        captured["token"] = token
        captured["timeout"] = timeout
        from robomp.git_ops import PrWorktreeResult

        return PrWorktreeResult(head=expected_head_sha, hydrated_paths=("src/changed.txt",))

    monkeypatch.setattr(proxy_server, "git_prepare_pr_worktree", fake_prepare_pr_worktree)
    monkeypatch.setattr(proxy_server, "_assert_origin_safe_for_repo", lambda *args, **kwargs: None)
    app = _build_app(proxy_settings)
    body = (
        b'{"repo":"test/widget","workspace_key":"test__widget__7__1111111111111111111111111111111111111111","pr_number":7,'
        b'"expected_head_sha":"1111111111111111111111111111111111111111","base_ref":"main","changed_paths":["src/changed.txt"]}'
    )
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/prepare_pr_worktree",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/prepare_pr_worktree", body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 200, resp.text
    assert captured["timeout"] == 37.0
    assert captured["changed_paths"] == ["src/changed.txt"]
    assert captured["expected_head_sha"] == "1" * 40


async def test_git_prepare_pr_worktree_rejects_missing_expected_head_sha(proxy_settings: ProxySettings) -> None:
    app = _build_app(proxy_settings)
    body = b'{"repo":"test/widget","workspace_key":"test__widget__7","pr_number":7,"base_ref":"main","changed_paths":["src/changed.txt"]}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/prepare_pr_worktree",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/prepare_pr_worktree", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 400


async def test_git_prepare_pr_worktree_rejects_invalid_expected_head_sha(proxy_settings: ProxySettings) -> None:
    app = _build_app(proxy_settings)
    body = b'{"repo":"test/widget","workspace_key":"test__widget__7","pr_number":7,"expected_head_sha":"abc123","base_ref":"main","changed_paths":["src/changed.txt"]}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/prepare_pr_worktree",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/prepare_pr_worktree", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 400


async def test_git_fetch_pr_head_uses_proxy_timeout_setting(
    proxy_settings: ProxySettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from robomp.proxy import server as proxy_server

    captured: dict[str, object] = {}
    proxy_settings.gh_proxy_git_timeout_seconds = 42.0

    def fake_fetch_pr_head(target: Path, pr_number: int, *, token: str | None, timeout: float | None = None) -> None:
        captured["target"] = target
        captured["pr_number"] = pr_number
        captured["token"] = token
        captured["timeout"] = timeout

    monkeypatch.setattr(proxy_server, "git_fetch_pr_head", fake_fetch_pr_head)
    app = _build_app(proxy_settings)
    body = b'{"repo":"octo/widget","pr_number":8177}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/fetch_pr_head",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/fetch_pr_head", body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 200, resp.text
    assert captured["pr_number"] == 8177
    assert captured["timeout"] == 37.0


async def test_git_push_happy_path(proxy_settings: ProxySettings, upstream_repo: Path) -> None:
    branch = "farm/abc/feature"
    _, head = _stage_workspace(proxy_settings, upstream_repo, "test/widget", 1, branch)
    # Rewire origin to the bare upstream so the proxy's push lands there.
    app = _build_app(proxy_settings)
    body = (
        b'{"repo":"test/widget","workspace_key":"test__widget__1","branch":"'
        + branch.encode()
        + b'","expected_head":"'
        + head.encode()
        + b'"}'
    )
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/push",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/push", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"head": head, "branch": branch}
    assert _bare_has_branch(upstream_repo, branch)


async def test_git_push_passes_slot_uid_to_git_push(
    proxy_settings: ProxySettings, upstream_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from robomp.git_ops import PushResult

    branch = "farm/abc/slot"
    repo_dir, head = _stage_workspace(proxy_settings, upstream_repo, "test/widget", 1, branch)
    slot_uid = 2001
    # The push handler reads the origin URL as the slot uid. On Linux+root
    # the staged workspace is root-owned; hand it to slot 2001 so the
    # subprocess can stat it. On macOS dev this is a no-op (slot identity
    # is never activated), so use the actual owner for the new owner check.
    if platform.system() == "Linux" and os.geteuid() == 0:
        for path in [repo_dir.parent, repo_dir, *repo_dir.rglob("*")]:
            os.chown(path, slot_uid, slot_uid, follow_symlinks=False)
    else:
        slot_uid = repo_dir.stat().st_uid
    captured: dict[str, object] = {}

    def fake_git_push(path: Path, **kwargs: object) -> PushResult:
        captured["path"] = path
        captured.update(kwargs)
        return PushResult(head=head, branch=branch)

    monkeypatch.setattr("robomp.proxy.server.git_push", fake_git_push)
    app = _build_app(proxy_settings)
    body = json.dumps(
        {
            "repo": "test/widget",
            "workspace_key": "test__widget__1",
            "branch": branch,
            "expected_head": head,
            "slot_uid": slot_uid,
        }
    ).encode()
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/push",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/push", body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 200, resp.text
    assert captured["path"] == repo_dir
    assert captured["slot_uid"] == slot_uid


@pytest.mark.parametrize("slot_uid", [0, -1, 65536])
async def test_git_push_rejects_invalid_slot_uid(proxy_settings: ProxySettings, slot_uid: int) -> None:
    app = _build_app(proxy_settings)
    body = (
        b'{"repo":"octo/widget","workspace_key":"octo__widget__1","branch":"x","expected_head":"'
        + (b"0" * 40)
        + b'","slot_uid":'
        + str(slot_uid).encode()
        + b"}"
    )
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/push",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/push", body), "Content-Type": "application/json"},
        )

    assert resp.status_code == 400
    assert "slot_uid" in resp.text


async def test_git_push_rejects_slot_owner_mismatch(proxy_settings: ProxySettings, upstream_repo: Path) -> None:
    branch = "farm/abc/slot-owner"
    repo_dir, head = _stage_workspace(proxy_settings, upstream_repo, "test/widget", 1, branch)
    wrong_uid = repo_dir.stat().st_uid + 1
    if wrong_uid >= 65536:
        wrong_uid = repo_dir.stat().st_uid - 1
    app = _build_app(proxy_settings)
    body = json.dumps(
        {
            "repo": "test/widget",
            "workspace_key": "test__widget__1",
            "branch": branch,
            "expected_head": head,
            "slot_uid": wrong_uid,
        }
    ).encode()
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/push",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/push", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    assert "slot_uid does not match workspace owner" in resp.text


async def test_git_push_rejects_mismatched_slot_uid(proxy_settings: ProxySettings, upstream_repo: Path) -> None:
    await test_git_push_rejects_slot_owner_mismatch(proxy_settings, upstream_repo)


async def test_git_push_rejects_invalid_branch_before_workspace_lookup(proxy_settings: ProxySettings) -> None:
    app = _build_app(proxy_settings)
    body = json.dumps(
        {
            "repo": "test/widget",
            "workspace_key": "test__widget__1",
            "branch": "-bad",
            "expected_head": "0" * 40,
        }
    ).encode()
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/push",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/push", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    assert "branch" in resp.text


async def test_git_push_head_drift(proxy_settings: ProxySettings, upstream_repo: Path) -> None:
    branch = "farm/abc/drift"
    _, _ = _stage_workspace(proxy_settings, upstream_repo, "test/widget", 1, branch)
    app = _build_app(proxy_settings)
    fake_head = "0" * 40
    body = (
        b'{"repo":"test/widget","workspace_key":"test__widget__1","branch":"'
        + branch.encode()
        + b'","expected_head":"'
        + fake_head.encode()
        + b'"}'
    )
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/push",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/push", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["kind"] == "head_drift"
    assert not _bare_has_branch(upstream_repo, branch)


async def test_git_push_workspace_key_mismatch(proxy_settings: ProxySettings) -> None:
    app = _build_app(proxy_settings)
    body = (
        b'{"repo":"octo/widget","workspace_key":"other__repo__1","branch":"x","expected_head":"' + (b"0" * 40) + b'"}'
    )
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/push",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/push", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    assert "workspace_key" in resp.text


# ============================================================================
# Finding 2 — HMAC must bind the raw query string
# ============================================================================


async def test_hmac_rejects_query_mutation(proxy_settings: ProxySettings) -> None:
    """Sign `/gh/v1/issue?repo=octo/widget&number=1`, replay with number=2.

    The verifier MUST notice the mutated query and 401. Without binding the
    query into the canonical string this request would sail through with an
    attacker-chosen target issue.
    """
    captured: list[httpx.Request] = []

    def gh(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200,
            json={
                "number": int(req.url.params["number"]),
                "title": "T",
                "body": "B",
                "state": "open",
                "user": {"login": "x"},
                "labels": [],
            },
        )

    app = _build_app(proxy_settings, gh)
    legit_params = {"repo": "octo/widget", "number": 1}
    headers = _signed("GET", "/gh/v1/issue", params=legit_params)
    mutated = {"repo": "octo/widget", "number": 2}
    async with await _async_client(app) as client:
        resp = await client.get("/gh/v1/issue", params=mutated, headers=headers)
    assert resp.status_code == 401, resp.text
    # Upstream GitHub mock MUST NOT have been called — auth failed first.
    assert captured == []


# ============================================================================
# Finding 3 — body must be size-capped BEFORE auth / before full buffer
# ============================================================================


async def test_oversized_content_length_rejected_with_413(proxy_settings: ProxySettings) -> None:
    """Setting Content-Length above the cap is rejected at 413 cheaply.

    With the fix in place the proxy never reads the (huge) body into memory:
    we declare CL > max_bytes and the handler aborts immediately. We force
    a tiny cap so the test stays fast; the production default is 1 MiB.
    """
    proxy_settings.gh_proxy_max_body_bytes = 256  # type: ignore[misc]
    app = _build_app(proxy_settings, lambda _: httpx.Response(500, json={}))
    payload = b"x" * 1024
    headers = {
        **_signed("POST", "/gh/v1/post_comment", payload),
        "Content-Type": "application/json",
        # Lie about CL to prove the early-reject path doesn't read content.
        "Content-Length": str(1024 * 1024 * 64),
    }
    async with await _async_client(app) as client:
        resp = await client.post("/gh/v1/post_comment", content=payload, headers=headers)
    assert resp.status_code == 413, resp.text


async def test_streamed_body_above_cap_rejected_with_413(proxy_settings: ProxySettings) -> None:
    """When Content-Length is honest but > cap, we still 413."""
    proxy_settings.gh_proxy_max_body_bytes = 64  # type: ignore[misc]
    app = _build_app(proxy_settings, lambda _: httpx.Response(500, json={}))
    payload = b'{"repo":"octo/widget","number":1,"body":"' + (b"y" * 200) + b'"}'
    headers = {
        **_signed("POST", "/gh/v1/post_comment", payload),
        "Content-Type": "application/json",
    }
    async with await _async_client(app) as client:
        resp = await client.post("/gh/v1/post_comment", content=payload, headers=headers)
    assert resp.status_code == 413


# ============================================================================
# Finding 5 — push refuses attacker-controlled origin (PAT exfil guard)
# ============================================================================


async def test_git_push_rejects_attacker_origin(proxy_settings: ProxySettings, upstream_repo: Path) -> None:
    """If the worktree's origin is rewritten to a non-github HTTPS URL,
    the push endpoint MUST refuse with 400 BEFORE invoking `git push` (which
    would carry the PAT to the attacker's host)."""
    branch = "farm/abc/evil"
    repo_dir, head = _stage_workspace(proxy_settings, upstream_repo, "octo/widget", 1, branch)
    # Simulate the agent rewriting origin inside its sandbox worktree.
    _git(["-C", str(repo_dir), "remote", "set-url", "origin", "https://evil.example.com/octo/widget.git"], repo_dir)

    app = _build_app(proxy_settings)
    body = (
        b'{"repo":"octo/widget","workspace_key":"octo__widget__1","branch":"'
        + branch.encode()
        + b'","expected_head":"'
        + head.encode()
        + b'"}'
    )
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/push",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/push", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 400, resp.text
    # The legit upstream never received the branch — proves push wasn't run.
    assert not _bare_has_branch(upstream_repo, branch)


async def test_git_push_rejects_origin_with_wrong_repo(proxy_settings: ProxySettings, upstream_repo: Path) -> None:
    """github.com host is not enough — owner/repo MUST match the request."""
    branch = "farm/abc/mismatch"
    repo_dir, head = _stage_workspace(proxy_settings, upstream_repo, "octo/widget", 1, branch)
    _git(["-C", str(repo_dir), "remote", "set-url", "origin", "https://github.com/attacker/other.git"], repo_dir)

    app = _build_app(proxy_settings)
    body = (
        b'{"repo":"octo/widget","workspace_key":"octo__widget__1","branch":"'
        + branch.encode()
        + b'","expected_head":"'
        + head.encode()
        + b'"}'
    )
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/push",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/push", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 400, resp.text
    assert not _bare_has_branch(upstream_repo, branch)


async def test_git_fetch_pr_head_rejects_bool_pr_number(proxy_settings: ProxySettings) -> None:
    """JSON true for pr_number should be rejected with HTTP 400."""
    app = _build_app(proxy_settings)
    body = json.dumps({"repo": "octo/widget", "pr_number": True}).encode()
    headers = _signed("POST", "/gh/v1/git/fetch_pr_head", body, key=_HMAC.encode())
    async with await _async_client(app) as client:
        resp = await client.post("/gh/v1/git/fetch_pr_head", content=body, headers=headers)
    assert resp.status_code == 400
    assert "pr_number" in resp.text


async def test_git_push_uses_proxy_timeout_setting(
    proxy_settings: ProxySettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git_push should receive timeout=_git_subprocess_timeout() from proxy settings."""
    from robomp.proxy import server as proxy_server

    captured: dict[str, Any] = {}

    def mock_git_push(*args: Any, **kwargs: Any) -> Any:
        from robomp.git_ops import PushResult
        captured.update(kwargs)
        return PushResult(head="abc123", branch="test-branch")

    def mock_workspace_repo_dir(*args: Any) -> Path:
        # Mock workspace directory that "exists"
        return Path("/fake/workspace/test__widget__123/repo")

    def mock_is_dir(self) -> bool:
        return True

    def mock_assert_origin_safe(*args: Any, **kwargs: Any) -> None:
        # Mock the origin safety check to do nothing
        return None

    monkeypatch.setattr(proxy_server, "git_push", mock_git_push)
    monkeypatch.setattr(proxy_server, "_workspace_repo_dir", mock_workspace_repo_dir)
    monkeypatch.setattr("pathlib.Path.is_dir", mock_is_dir)
    monkeypatch.setattr(proxy_server, "_assert_origin_safe_for_repo", mock_assert_origin_safe)
    proxy_settings.gh_proxy_git_timeout_seconds = 42.0

    app = _build_app(proxy_settings)
    body = json.dumps({
        "repo": "test/widget",
        "workspace_key": "test__widget__123",
        "branch": "test-branch",
        "expected_head": "abc123",
    }).encode()
    headers = _signed("POST", "/gh/v1/git/push", body, key=_HMAC.encode())
    async with await _async_client(app) as client:
        resp = await client.post("/gh/v1/git/push", content=body, headers=headers)

    assert resp.status_code == 200, resp.text
    assert captured["timeout"] == 37.0
