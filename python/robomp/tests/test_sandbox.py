from __future__ import annotations

import json
import os
import platform
import signal
import stat
import subprocess
from pathlib import Path

import pytest

from robomp.git_ops import (
    GitCommandError,
)
from robomp.git_ops import (
    fetch_pr_head as git_fetch_pr_head,
    prepare_pr_worktree as git_prepare_pr_worktree,
)
from robomp.git_ops import (
    fetch_prune as git_fetch_prune,
)
from robomp.git_ops import (
    fetch_ref as git_fetch_ref,
)
from robomp.git_ops import (
    normalize_pr_sparse_paths,
    pr_sparse_checkout_patterns,
)
from robomp.sandbox import (
    SandboxManager,
    Workspace,
    _chown_workspace,
    _prepare_slot_runtime_env,
    _prepare_slot_tmpdir,
    _provision_runtime_dirs,
    _reap_slot,
    _safe_directory_env,
    _share_git_metadata_with_slots,
    _slot_pids,
    _slot_subprocess_kwargs,
    make_branch,
    pr_review_workspace_key,
    rename_workspace_branch,
    workspace_key,
)


def test_pr_review_workspace_key_includes_full_head_sha() -> None:
    assert workspace_key("oven-sh/bun", 30654) == "oven-sh__bun__30654"
    assert pr_review_workspace_key("oven-sh/bun", 30654, "A" * 40) == "oven-sh__bun__30654__" + ("a" * 40)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _workspace(root: Path) -> Workspace:
    return Workspace(
        root=root,
        repo_dir=root / "repo",
        session_dir=root / ".omp-session",
        context_dir=root / "context",
        artifacts_dir=root / "artifacts",
        branch="farm/test/topic",
        repo_full_name="octo/widget",
        issue_number=1,
    )


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Path:
    """Create a local --bare-ish remote with one commit on main."""
    repo = tmp_path / "upstream.git"
    repo.mkdir()
    _git(["init", "--initial-branch=main", "--bare", str(repo)], cwd=tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "--initial-branch=main", str(seed)], cwd=tmp_path)
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["-C", str(seed), "add", "."], cwd=tmp_path)
    env = os.environ | {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(seed),
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    _git(["-C", str(seed), "remote", "add", "origin", str(repo)], cwd=tmp_path)
    _git(["-C", str(seed), "push", "origin", "main"], cwd=tmp_path)
    return repo


def test_workspace_key_and_branch_shape() -> None:
    assert workspace_key("oven-sh/bun", 30654) == "oven-sh__bun__30654"
    branch = make_branch(issue_number=30654, title="JSON.parse crashes on BOM", seed="oven-sh/bun#30654")
    assert branch.startswith("farm/")
    parts = branch.split("/")
    assert len(parts) == 3 and len(parts[1]) == 8
    assert "json-parse-crashes" in parts[2]


def _init_worktree_repo(repo_dir: Path, branch: str) -> None:
    """Stand up a minimal local git repo with `branch` checked out."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(["init", f"--initial-branch={branch}", str(repo_dir)], cwd=repo_dir.parent)
    (repo_dir / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["-C", str(repo_dir), "add", "."], cwd=repo_dir.parent)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo_dir),
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


def test_rename_workspace_branch_renames_local_branch(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    repo_dir = root / "repo"
    initial = "farm/abc12345/some-issue"
    _init_worktree_repo(repo_dir, initial)
    ws = Workspace(
        root=root,
        repo_dir=repo_dir,
        session_dir=root / ".omp-session",
        context_dir=root / "context",
        artifacts_dir=root / "artifacts",
        branch=initial,
        repo_full_name="octo/widget",
        issue_number=1,
    )
    new_branch = rename_workspace_branch(ws, "fix-json-bom")
    assert new_branch == "farm/abc12345/fix-json-bom"
    assert ws.branch == "farm/abc12345/fix-json-bom"
    head = subprocess.run(
        ["git", "symbolic-ref", "HEAD"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == "refs/heads/farm/abc12345/fix-json-bom"


def test_rename_workspace_branch_refreshes_shared_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "ws"
    repo_dir = root / "repo"
    initial = "farm/abc12345/some-issue"
    _init_worktree_repo(repo_dir, initial)
    ws = Workspace(
        root=root,
        repo_dir=repo_dir,
        session_dir=root / ".omp-session",
        context_dir=root / "context",
        artifacts_dir=root / "artifacts",
        branch=initial,
        repo_full_name="octo/widget",
        issue_number=1,
    )
    # On Linux+root the rename runs `git branch -m` as the slot uid (2004),
    # so the worktree needs to be readable by that uid before the call.
    # On macOS dev `_slot_permissions_active` returns False and this
    # whole block is a no-op.
    if platform.system() == "Linux" and os.geteuid() == 0:
        for path in [root, repo_dir, *repo_dir.rglob("*")]:
            os.chown(path, 2004, 2004, follow_symlinks=False)
    calls: list[tuple[Path, int | None]] = []
    monkeypatch.setattr(
        "robomp.sandbox._share_git_metadata_with_slots",
        lambda repo_dir, slot_uid: calls.append((repo_dir, slot_uid)),
    )

    rename_workspace_branch(ws, "fix-json-bom", slot_uid=2004)

    assert calls == [(repo_dir, 2004)]


def test_rename_workspace_branch_runs_git_as_slot_when_permissions_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ws"
    repo_dir = root / "repo"
    repo_dir.mkdir(parents=True)
    initial = "farm/abc12345/some-issue"
    ws = Workspace(
        root=root,
        repo_dir=repo_dir,
        session_dir=root / ".omp-session",
        context_dir=root / "context",
        artifacts_dir=root / "artifacts",
        branch=initial,
        repo_full_name="octo/widget",
        issue_number=1,
    )
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("robomp.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("robomp.sandbox.os.geteuid", lambda: 0)
    monkeypatch.setattr("robomp.sandbox.subprocess.run", fake_run)
    monkeypatch.setattr("robomp.sandbox._share_git_metadata_with_slots", lambda _repo_dir, _slot_uid: None)

    new_branch = rename_workspace_branch(ws, "fix-json-bom", slot_uid=2004)

    assert new_branch == "farm/abc12345/fix-json-bom"
    assert captured["cmd"] == ["git", "branch", "-m", initial, "farm/abc12345/fix-json-bom"]
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["cwd"] == str(repo_dir)
    assert kwargs["user"] == 2004
    assert kwargs["group"] == 2004
    assert kwargs["extra_groups"] == [2000]


def test_rename_workspace_branch_is_idempotent_when_slug_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    repo_dir = root / "repo"
    initial = "farm/abc12345/keep-me"
    _init_worktree_repo(repo_dir, initial)
    ws = Workspace(
        root=root,
        repo_dir=repo_dir,
        session_dir=root / ".omp-session",
        context_dir=root / "context",
        artifacts_dir=root / "artifacts",
        branch=initial,
        repo_full_name="octo/widget",
        issue_number=1,
    )
    # No git operation should run; nothing to rename. We assert that by
    # passing a non-existent repo_dir — the helper must not touch git.
    ws.repo_dir = tmp_path / "does-not-exist"
    out = rename_workspace_branch(ws, "keep-me")
    assert out == initial
    assert ws.branch == initial


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "Has-Caps",
        "-leading",
        "trailing-",
        "double--hyphen",
        "has/slash",
        "has_underscore",
        "a" * 51,
        None,
        123,
    ],
)
def test_rename_workspace_branch_rejects_bad_slug(tmp_path: Path, bad: object) -> None:
    ws = _workspace(tmp_path / "ws")
    with pytest.raises(ValueError):
        rename_workspace_branch(ws, bad)  # type: ignore[arg-type]


def test_rename_workspace_branch_noop_when_pr_open(tmp_path: Path) -> None:
    """A non-None ``pr_number`` makes rename a no-op: an open PR on origin
    still tracks the current branch, and renaming would orphan it."""
    root = tmp_path / "ws"
    repo_dir = root / "repo"
    initial = "farm/abc12345/old-slug"
    _init_worktree_repo(repo_dir, initial)
    ws = Workspace(
        root=root,
        repo_dir=repo_dir,
        session_dir=root / ".omp-session",
        context_dir=root / "context",
        artifacts_dir=root / "artifacts",
        branch=initial,
        repo_full_name="octo/widget",
        issue_number=1,
    )
    out = rename_workspace_branch(ws, "new-slug", pr_number=42)
    assert out == initial
    assert ws.branch == initial
    head = subprocess.run(
        ["git", "symbolic-ref", "HEAD"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == f"refs/heads/{initial}"
    # An invalid slug must still be rejected even when pr_number suppresses the rename.
    with pytest.raises(ValueError):
        rename_workspace_branch(ws, "Bad Slug", pr_number=42)


def test_rename_workspace_branch_rejects_non_farm_branch(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws")
    ws.branch = "main"
    with pytest.raises(ValueError):
        rename_workspace_branch(ws, "ok-slug")


def test_rename_workspace_branch_surfaces_git_failure(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    repo_dir = root / "repo"
    initial = "farm/abc12345/old"
    _init_worktree_repo(repo_dir, initial)
    # Create a second branch that collides with the rename target.
    _git(["-C", str(repo_dir), "branch", "farm/abc12345/new"], cwd=repo_dir.parent)
    ws = Workspace(
        root=root,
        repo_dir=repo_dir,
        session_dir=root / ".omp-session",
        context_dir=root / "context",
        artifacts_dir=root / "artifacts",
        branch=initial,
        repo_full_name="octo/widget",
        issue_number=1,
    )
    with pytest.raises(GitCommandError):
        rename_workspace_branch(ws, "new")
    # Original branch must remain on failure.
    assert ws.branch == initial


def test_delete_bad_refs_removes_worktree_holding_the_ref(tmp_path: Path) -> None:
    """When a bad-object ref is still checked out by a worktree, the worktree's
    stale ``HEAD`` keeps fetch failing even after ``update-ref -d`` succeeds.
    The repair MUST also tear down that worktree before deleting the ref."""
    from robomp.git_ops import _delete_bad_refs

    pool = tmp_path / "pool"
    _init_worktree_repo(pool, "main")

    # Create a worktree on `farm/badhex/bad-branch`.
    work_dir = tmp_path / "worktree"
    subprocess.run(
        ["git", "worktree", "add", "-b", "farm/badhex/bad-branch", str(work_dir), "main"],
        cwd=str(pool),
        check=True,
        capture_output=True,
        text=True,
    )
    assert (work_dir / ".git").exists()

    fetch_output = (
        "error: object directory /tmp/git-objects-aux does not exist; check .git/objects/info/alternates\n"
        "fatal: bad object refs/heads/farm/badhex/bad-branch\n"
        "error: did not send all necessary objects\n"
    )
    changed = _delete_bad_refs(pool, fetch_output)
    assert changed is True
    # Ref must be gone from the pool's refs store.
    rp = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/heads/farm/badhex/bad-branch"],
        cwd=str(pool),
        capture_output=True,
        text=True,
    )
    assert rp.returncode != 0
    # And the worktree's `.git` link must be cleared so the next fetch can
    # validate connectivity without re-tripping over the dead HEAD.
    assert not (work_dir / ".git").exists()


def test_delete_bad_refs_noop_when_no_bad_ref_in_output(tmp_path: Path) -> None:
    from robomp.git_ops import _delete_bad_refs

    pool = tmp_path / "pool"
    _init_worktree_repo(pool, "main")
    # Output that doesn't match the bad-object regex.
    assert _delete_bad_refs(pool, "fatal: unrelated failure\n") is False


def test_ensure_workspace_creates_worktree(tmp_path: Path, upstream_repo: Path) -> None:
    mgr = SandboxManager(tmp_path / "workspaces")
    ws = mgr.ensure_workspace(
        repo="octo/widget",
        number=42,
        title="something is wrong",
        clone_url=str(upstream_repo),
        default_branch="main",
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    assert ws.repo_dir.is_dir()
    assert (ws.repo_dir / "README.md").read_text() == "hello\n"
    # Branch is checked out.
    result = subprocess.run(
        ["git", "-C", str(ws.repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ws.branch
    assert ws.branch.startswith("farm/")
    # Session and context dirs exist.
    assert ws.session_dir.is_dir()
    assert ws.context_dir.is_dir()
    assert ws.repro_dir.is_dir()
    assert ws.artifacts_dir.is_dir()


def test_ensure_workspace_pr_head_uses_detached_pr_ref(tmp_path: Path, upstream_repo: Path) -> None:
    contributor = tmp_path / "contributor"
    _git(["clone", str(upstream_repo), str(contributor)], cwd=tmp_path)
    (contributor / "README.md").write_text("hello from pr\n", encoding="utf-8")
    _git(["-C", str(contributor), "add", "README.md"], cwd=tmp_path)
    subprocess.run(
        ["git", "commit", "-m", "pr change"],
        cwd=str(contributor),
        check=True,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "GIT_AUTHOR_NAME": "c",
            "GIT_AUTHOR_EMAIL": "c@t",
            "GIT_COMMITTER_NAME": "c",
            "GIT_COMMITTER_EMAIL": "c@t",
        },
    )
    pr_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(contributor),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(["-C", str(contributor), "push", "origin", "HEAD:refs/pull/9/head"], cwd=tmp_path)

    mgr = SandboxManager(tmp_path / "workspaces")
    ws = mgr.ensure_workspace(
        repo="octo/widget",
        number=9,
        title="incoming PR",
        clone_url=str(upstream_repo),
        default_branch="main",
        pr_head=9,
        pr_base_ref="main",
        pr_changed_paths=("README.md",),
        pr_head_sha=pr_head,
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(ws.repo_dir),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    symbolic = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=str(ws.repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    pushurl = subprocess.run(
        ["git", "config", "--get", "remote.origin.pushurl"],
        cwd=str(ws.repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    assert head == pr_head
    assert symbolic.returncode != 0
    assert ws.branch == "review/pr-9"
    assert pushurl.returncode != 0
    assert ws.review_head_sha == pr_head



def _publish_pr_readme(upstream: Path, tmp_path: Path, *, ref: str, content: str, clone_name: str) -> str:
    contributor = tmp_path / clone_name
    _git(["clone", str(upstream), str(contributor)], cwd=tmp_path)
    (contributor / "README.md").write_text(content, encoding="utf-8")
    _git(["-C", str(contributor), "add", "README.md"], cwd=tmp_path)
    subprocess.run(
        ["git", "commit", "-m", "pr change"],
        cwd=str(contributor),
        check=True,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "GIT_AUTHOR_NAME": "c",
            "GIT_AUTHOR_EMAIL": "c@t",
            "GIT_COMMITTER_NAME": "c",
            "GIT_COMMITTER_EMAIL": "c@t",
        },
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(contributor),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(["-C", str(contributor), "push", "--force", "origin", f"HEAD:{ref}"], cwd=tmp_path)
    return sha


def test_ensure_workspace_pr_head_scopes_session_by_head(tmp_path: Path, upstream_repo: Path) -> None:
    sha1 = _publish_pr_readme(upstream_repo, tmp_path, ref="refs/pull/7/head", content="head one\n", clone_name="pr-one")
    mgr = SandboxManager(tmp_path / "workspaces")
    ws1 = mgr.ensure_workspace(
        repo="octo/widget",
        number=7,
        title="incoming PR",
        clone_url=str(upstream_repo),
        default_branch="main",
        pr_head=7,
        pr_head_sha=sha1,
        pr_base_ref="main",
        pr_changed_paths=("README.md",),
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    dummy = ws1.session_dir / "turn.jsonl"
    dummy.write_text("{}\n", encoding="utf-8")

    sha2 = _publish_pr_readme(upstream_repo, tmp_path, ref="refs/pull/7/head", content="head two\n", clone_name="pr-two")
    ws2 = mgr.ensure_workspace(
        repo="octo/widget",
        number=7,
        title="incoming PR",
        clone_url=str(upstream_repo),
        default_branch="main",
        pr_head=7,
        pr_head_sha=sha2,
        pr_base_ref="main",
        pr_changed_paths=("README.md",),
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    head2 = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(ws2.repo_dir),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert ws2.root != ws1.root
    assert ws2.session_dir != ws1.session_dir
    assert ws2.review_head_sha == sha2
    assert head2 == sha2
    assert dummy.is_file()
    assert not (ws2.session_dir / "turn.jsonl").exists()


def test_ensure_workspace_recreates_corrupt_same_head_pr_workspace(tmp_path: Path, upstream_repo: Path) -> None:
    sha = _publish_pr_readme(upstream_repo, tmp_path, ref="refs/pull/7/head", content="head one\n", clone_name="pr-corrupt")
    mgr = SandboxManager(tmp_path / "workspaces")
    ws1 = mgr.ensure_workspace(
        repo="octo/widget",
        number=7,
        title="incoming PR",
        clone_url=str(upstream_repo),
        default_branch="main",
        pr_head=7,
        pr_head_sha=sha,
        pr_base_ref="main",
        pr_changed_paths=("README.md",),
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    manifest_path = ws1.session_dir / "pr-review-workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["head_sha"] = "0" * 40
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    ws2 = mgr.ensure_workspace(
        repo="octo/widget",
        number=7,
        title="incoming PR",
        clone_url=str(upstream_repo),
        default_branch="main",
        pr_head=7,
        pr_head_sha=sha,
        pr_base_ref="main",
        pr_changed_paths=("README.md",),
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(ws2.repo_dir),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    rewritten = json.loads((ws2.session_dir / "pr-review-workspace.json").read_text(encoding="utf-8"))
    assert ws2.root == ws1.root
    assert head == sha
    assert rewritten["head_sha"] == sha


def test_chown_workspace_noops_when_not_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], bool]] = []

    monkeypatch.setattr("robomp.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("robomp.sandbox.os.geteuid", lambda: 1000)
    monkeypatch.setattr(
        "robomp.sandbox.subprocess.run",
        lambda cmd, *, check: calls.append((cmd, check)),
    )

    _chown_workspace(tmp_path, 2001)

    assert calls == []


def test_chown_workspace_noops_off_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], bool]] = []

    monkeypatch.setattr("robomp.sandbox.platform.system", lambda: "Darwin")
    monkeypatch.setattr("robomp.sandbox.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "robomp.sandbox.subprocess.run",
        lambda cmd, *, check: calls.append((cmd, check)),
    )

    _chown_workspace(tmp_path, 2001)

    assert calls == []


def test_chown_workspace_runs_chown_and_chmod_as_root_on_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chowns: list[tuple[Path, int, int, bool | None]] = []
    chmods: list[tuple[Path, int, bool | None]] = []

    def fake_chown(path: Path, uid: int, gid: int, *, follow_symlinks: bool | None = None) -> None:
        chowns.append((Path(path), uid, gid, follow_symlinks))

    def fake_chmod(path: Path, mode: int, *, follow_symlinks: bool | None = None) -> None:
        chmods.append((Path(path), mode, follow_symlinks))

    monkeypatch.setattr("robomp.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("robomp.sandbox.os.geteuid", lambda: 0)
    monkeypatch.setattr("robomp.sandbox.os.chown", fake_chown)
    monkeypatch.setattr("robomp.sandbox.os.chmod", fake_chmod)
    monkeypatch.setattr("robomp.sandbox._ensure_root_control_dir", lambda _path: None)
    monkeypatch.setattr("robomp.sandbox._chown_slot_leaves", lambda _root, _slot_uid: None)

    _chown_workspace(tmp_path, 2001)

    assert (tmp_path, 2001, 2001, False) in chowns
    assert (tmp_path, 0o770, False) in chmods


def test_chown_workspace_makes_workspace_slot_owned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    file_path = subdir / "file.txt"
    file_path.write_text("data\n", encoding="utf-8")
    control = tmp_path / ".omp-session"
    control.mkdir()
    control_file = control / "turn.jsonl"
    control_file.write_text("{}\n", encoding="utf-8")
    owned: dict[Path, tuple[int, int]] = {}
    real_chmod = os.chmod

    def fake_chown(path: Path, uid: int, gid: int, *, follow_symlinks: bool | None = None) -> None:
        assert follow_symlinks is False
        owned[Path(path)] = (uid, gid)

    def fake_chmod(path: Path, mode: int, *, follow_symlinks: bool | None = None) -> None:
        assert follow_symlinks is False
        real_chmod(path, mode, follow_symlinks=False)

    monkeypatch.setattr("robomp.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("robomp.sandbox.os.geteuid", lambda: 0)
    monkeypatch.setattr("robomp.sandbox.os.chown", fake_chown)
    monkeypatch.setattr("robomp.sandbox.os.chmod", fake_chmod)
    monkeypatch.setattr("robomp.sandbox._ensure_root_control_dir", lambda _path: None)
    monkeypatch.setattr("robomp.sandbox._chown_slot_leaves", lambda _root, _slot_uid: None)

    _chown_workspace(tmp_path, 2001)

    assert owned[tmp_path] == (2001, 2001)
    assert owned[subdir] == (2001, 2001)
    assert owned[file_path] == (2001, 2001)
    assert control not in owned
    assert control_file not in owned
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o770
    assert stat.S_IMODE(subdir.stat().st_mode) == 0o770
    assert stat.S_IMODE(file_path.stat().st_mode) == 0o660


def test_slot_pids_reads_proc_status_and_skips_zombies(tmp_path: Path) -> None:
    nonnumeric = tmp_path / "self"
    nonnumeric.mkdir()

    live = tmp_path / "123"
    live.mkdir()
    (live / "status").write_text(
        "Name:\tomp\nState:\tS (sleeping)\nUid:\t0\t2001\t2001\t2001\n",
        encoding="utf-8",
    )

    zombie = tmp_path / "124"
    zombie.mkdir()
    (zombie / "status").write_text(
        "Name:\tomp\nState:\tZ (zombie)\nUid:\t2001\t2001\t2001\t2001\n",
        encoding="utf-8",
    )

    other = tmp_path / "125"
    other.mkdir()
    (other / "status").write_text(
        "Name:\troot\nState:\tS (sleeping)\nUid:\t0\t0\t0\t0\n",
        encoding="utf-8",
    )

    assert _slot_pids(2001, tmp_path) == (123,)


def test_reap_slot_noops_when_permissions_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    monkeypatch.setattr("robomp.sandbox.platform.system", lambda: "Darwin")
    monkeypatch.setattr("robomp.sandbox.os.geteuid", lambda: 0)
    monkeypatch.setattr("robomp.sandbox.os.kill", lambda pid, sig: calls.append((pid, sig)))

    _reap_slot(2001)

    assert calls == []


def test_reap_slot_kills_slot_uid_on_linux_root(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    monkeypatch.setattr("robomp.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("robomp.sandbox.os.geteuid", lambda: 0)
    monkeypatch.setattr("robomp.sandbox._slot_pids", lambda _uid: (111, 222))
    monkeypatch.setattr("robomp.sandbox.os.kill", lambda pid, sig: calls.append((pid, sig)))

    _reap_slot(2001)

    assert calls == [(111, signal.SIGKILL), (222, signal.SIGKILL)]


def test_prepare_slot_tmpdir_mkdirs_symlink_safe_slot_leaf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chowns: list[tuple[int, int, int]] = []

    monkeypatch.setattr("robomp.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("robomp.sandbox.os.geteuid", lambda: 0)
    monkeypatch.setattr("robomp.sandbox.os.fchown", lambda fd, uid, gid: chowns.append((fd, uid, gid)))

    tmpdir = _prepare_slot_tmpdir(_workspace(tmp_path), 2001)

    assert tmpdir == tmp_path / ".omp-tmp" / "slot-2001"
    assert tmpdir.is_dir()
    assert stat.S_IMODE((tmp_path / ".omp-tmp").stat().st_mode) == 0o755
    assert stat.S_IMODE(tmpdir.stat().st_mode) == 0o700
    assert (0, 0) in [(uid, gid) for _fd, uid, gid in chowns]
    assert (2001, 2001) in [(uid, gid) for _fd, uid, gid in chowns]


def test_prepare_slot_tmpdir_replaces_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    tmpdir = tmp_path / ".omp-tmp"
    tmpdir.symlink_to(target, target_is_directory=True)

    prepared = _prepare_slot_tmpdir(_workspace(tmp_path), None)

    assert prepared == tmpdir
    assert prepared.is_dir()
    assert not prepared.is_symlink()
    assert target.is_dir()


def test_provision_runtime_dirs_replaces_tmpdir_symlink_and_creates_xdg_tree(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    tmpdir = tmp_path / ".omp-tmp"
    tmpdir.symlink_to(target, target_is_directory=True)

    _provision_runtime_dirs(tmp_path)

    assert tmpdir.is_dir()
    assert not tmpdir.is_symlink()
    assert target.is_dir()
    assert stat.S_IMODE(tmpdir.stat().st_mode) == 0o755
    for base in (
        tmp_path / ".omp-xdg" / "data",
        tmp_path / ".omp-xdg" / "state",
        tmp_path / ".omp-xdg" / "cache",
        tmp_path / ".omp-xdg" / "config",
    ):
        assert base.is_dir()
    assert (tmp_path / ".omp-xdg" / "data" / "omp").is_dir()
    assert (tmp_path / ".omp-xdg" / "state" / "omp").is_dir()
    assert (tmp_path / ".omp-xdg" / "cache" / "omp").is_dir()
    assert (tmp_path / ".omp-xdg" / "cache" / "bun-install").is_dir()


def test_workspace_control_dirs_resist_symlink_swap(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    for relative in (".omp-session", "context", "artifacts", ".omp-tmp", ".omp-xdg"):
        link = tmp_path / relative
        link.symlink_to(target, target_is_directory=True)

    _provision_runtime_dirs(tmp_path)

    for relative in (".omp-session", "context", "artifacts", ".omp-tmp", ".omp-xdg"):
        control = tmp_path / relative
        assert control.is_dir()
        assert not control.is_symlink()
        assert stat.S_IMODE(control.stat().st_mode) == 0o755
    assert target.is_dir()


def test_safe_directory_env_scopes_single_repo_path(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"

    assert _safe_directory_env(repo_dir) == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(repo_dir),
    }


def test_slot_subprocess_kwargs_run_as_slot_on_linux_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("robomp.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("robomp.sandbox.os.geteuid", lambda: 0)

    assert _slot_subprocess_kwargs(2001) == {
        "user": 2001,
        "group": 2001,
        "extra_groups": [2000],
        "umask": 0o002,
    }


def test_chown_workspace_normalizes_to_root_when_slots_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chowns: list[tuple[Path, int, int]] = []

    monkeypatch.setattr("robomp.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("robomp.sandbox.os.geteuid", lambda: 0)
    monkeypatch.setattr("robomp.sandbox.os.getegid", lambda: 0)
    monkeypatch.setattr(
        "robomp.sandbox.os.chown",
        lambda path, uid, gid, *, follow_symlinks=False: chowns.append((Path(path), uid, gid)),
    )
    monkeypatch.setattr("robomp.sandbox.os.chmod", lambda path, mode, *, follow_symlinks=False: None)
    monkeypatch.setattr("robomp.sandbox._ensure_root_control_dir", lambda _path: None)

    _chown_workspace(tmp_path, None)

    assert chowns == [(tmp_path, 0, 0)]


def test_prepare_slot_runtime_env_returns_workspace_private_paths_without_path_chown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chowns: list[tuple[Path, int, int]] = []
    calls: list[list[str]] = []

    monkeypatch.setattr("robomp.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("robomp.sandbox.os.geteuid", lambda: 0)
    monkeypatch.setattr("robomp.sandbox.os.chown", lambda path, uid, gid: chowns.append((Path(path), uid, gid)))
    monkeypatch.setattr("robomp.sandbox.os.fchown", lambda _fd, _uid, _gid: None)
    monkeypatch.setattr("robomp.sandbox.subprocess.run", lambda cmd, **_kwargs: calls.append(cmd))

    ws = _workspace(tmp_path)
    bun_cache = ws.root / ".omp-xdg" / "cache" / "bun-install"

    env = _prepare_slot_runtime_env(ws, 2001)

    assert env["TMPDIR"] == str(ws.root / ".omp-tmp" / "slot-2001")
    assert env["XDG_CONFIG_HOME"] == str(ws.root / ".omp-xdg" / "config")
    assert env["XDG_CACHE_HOME"] == str(ws.root / ".omp-xdg" / "cache")
    assert env["BUN_INSTALL_CACHE_DIR"] == str(bun_cache)
    for base in (
        ws.root / ".omp-xdg" / "data",
        ws.root / ".omp-xdg" / "state",
        ws.root / ".omp-xdg" / "cache",
        ws.root / ".omp-xdg" / "config",
    ):
        assert base.is_dir()
    assert (ws.root / ".omp-xdg" / "data" / "omp").is_dir()
    assert (ws.root / ".omp-xdg" / "state" / "omp").is_dir()
    assert (ws.root / ".omp-xdg" / "cache" / "omp").is_dir()
    assert bun_cache.is_dir()
    assert chowns == []
    assert calls == []


def test_share_git_metadata_leaves_pool_metadata_root_owned_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_dir = tmp_path / "workspaces" / "octo__widget__43" / "repo"
    repo_dir.mkdir(parents=True)
    common_dir = tmp_path / "workspaces" / "_pool" / "octo__widget" / ".git"
    git_dir = common_dir / "worktrees" / "repo"
    git_dir.mkdir(parents=True)
    (repo_dir / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    ref_file = common_dir / "refs" / "heads" / "farm"
    ref_file.parent.mkdir(parents=True)
    ref_file.write_text("sha\n", encoding="utf-8")
    ref_file.chmod(0o600)

    chowns: list[tuple[Path, int, int]] = []
    monkeypatch.setattr("robomp.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("robomp.sandbox.os.geteuid", lambda: 0)
    monkeypatch.setattr("robomp.sandbox.os.chown", lambda path, uid, gid: chowns.append((Path(path), uid, gid)))

    _share_git_metadata_with_slots(repo_dir, 2002)

    assert stat.S_IMODE(ref_file.stat().st_mode) == 0o600
    assert chowns == []


def test_ensure_workspace_refreshes_permissions_for_retry_slot_and_session(
    tmp_path: Path, upstream_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chowns: list[tuple[Path, int | None]] = []
    shared: list[tuple[Path, int | None]] = []
    real_chown = _chown_workspace
    real_share = _share_git_metadata_with_slots

    def record_chown(root: Path, slot_uid: int | None) -> None:
        chowns.append((root, slot_uid))
        # Delegate so subsequent slot-identity git ops can stat the tree.
        real_chown(root, slot_uid)

    def record_share(repo_dir: Path, slot_uid: int | None) -> None:
        shared.append((repo_dir, slot_uid))
        real_share(repo_dir, slot_uid)

    monkeypatch.setattr("robomp.sandbox._chown_workspace", record_chown)
    monkeypatch.setattr("robomp.sandbox._share_git_metadata_with_slots", record_share)

    mgr = SandboxManager(tmp_path / "workspaces")
    ws1 = mgr.ensure_workspace(
        repo="octo/widget",
        number=44,
        title="retry me",
        clone_url=str(upstream_repo),
        default_branch="main",
        slot_uid=2001,
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    transcript = ws1.session_dir / "turn.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    ws2 = mgr.ensure_workspace(
        repo="octo/widget",
        number=44,
        title="retry me",
        clone_url=str(upstream_repo),
        default_branch="main",
        existing_branch=ws1.branch,
        slot_uid=2002,
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )

    assert ws2.repo_dir == ws1.repo_dir
    assert ws2.session_dir == ws1.session_dir
    assert transcript.is_file()
    assert ws2.branch == ws1.branch
    assert shared == [
        (ws1.repo_dir, 2001),
        (ws1.repo_dir, 2001),
        (ws1.repo_dir, 2002),
        (ws1.repo_dir, 2002),
    ]
    assert chowns == [(ws1.root, 2001), (ws1.root, 2002)]


def test_ensure_workspace_preserves_checked_out_branch_on_replay(tmp_path: Path, upstream_repo: Path) -> None:
    mgr = SandboxManager(tmp_path / "workspaces")
    ws1 = mgr.ensure_workspace(
        repo="octo/widget",
        number=45,
        title="retry me",
        clone_url=str(upstream_repo),
        default_branch="main",
        slot_uid=None,
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    renamed = "farm/abc12345/renamed"
    _git(["-C", str(ws1.repo_dir), "branch", "-m", ws1.branch, renamed], cwd=ws1.repo_dir.parent)

    ws2 = mgr.ensure_workspace(
        repo="octo/widget",
        number=45,
        title="retry me",
        clone_url=str(upstream_repo),
        default_branch="main",
        slot_uid=None,
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )

    assert ws2.branch == renamed


def test_ensure_workspace_runs_existing_worktree_git_as_slot_after_chown(
    tmp_path: Path, upstream_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = SandboxManager(tmp_path / "workspaces")
    ws1 = mgr.ensure_workspace(
        repo="octo/widget",
        number=47,
        title="retry me",
        clone_url=str(upstream_repo),
        default_branch="main",
        slot_uid=None,
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    events: list[tuple[str, int | None]] = []
    git_calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        git_calls.append((cmd, kwargs))
        if cmd[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(cmd, 0, f"{upstream_repo}\n", "")
        if cmd[:4] == ["git", "symbolic-ref", "--quiet", "--short"]:
            user = kwargs.get("user")
            events.append(("symbolic-ref", user if isinstance(user, int) else None))
            return subprocess.CompletedProcess(cmd, 0, f"{ws1.branch}\n", "")
        if cmd[:2] == ["git", "config"]:
            user = kwargs.get("user")
            events.append(("config", user if isinstance(user, int) else None))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def record_chown(_ws_root: Path, slot_uid: int | None) -> None:
        events.append(("chown", slot_uid))

    monkeypatch.setattr("robomp.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("robomp.sandbox.os.geteuid", lambda: 0)
    monkeypatch.setattr("robomp.sandbox.os.fchown", lambda _fd, _uid, _gid: None)
    monkeypatch.setattr("robomp.sandbox.subprocess.run", fake_run)
    monkeypatch.setattr("robomp.sandbox._chown_workspace", record_chown)
    monkeypatch.setattr("robomp.sandbox._share_git_metadata_with_slots", lambda _repo_dir, _slot_uid: None)

    ws2 = mgr.ensure_workspace(
        repo="octo/widget",
        number=47,
        title="retry me",
        clone_url=str(upstream_repo),
        default_branch="main",
        slot_uid=2002,
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )

    assert ws2.branch == ws1.branch
    assert events[0] == ("chown", 2002)
    assert ("symbolic-ref", 2002) in events
    assert events.index(("chown", 2002)) < events.index(("symbolic-ref", 2002))
    assert events.count(("config", 2002)) == 2
    worktree_git = [kwargs for cmd, kwargs in git_calls if cmd[:2] in (["git", "symbolic-ref"], ["git", "config"])]
    assert worktree_git
    assert all(kwargs["user"] == 2002 and kwargs["group"] == 2002 for kwargs in worktree_git)


def test_ensure_workspace_invokes_slot_chown(
    tmp_path: Path, upstream_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, int | None]] = []
    real_chown = _chown_workspace

    def record_chown(ws_root: Path, slot_uid: int | None) -> None:
        calls.append((ws_root, slot_uid))
        # Delegate to the real chown so the subsequent `git config` as the
        # slot can stat the tree. On macOS dev (uid != 0) the real chown is
        # itself a no-op; on Linux+root in CI it hands the tree to the slot.
        real_chown(ws_root, slot_uid)

    monkeypatch.setattr("robomp.sandbox._chown_workspace", record_chown)
    mgr = SandboxManager(tmp_path / "workspaces")

    ws = mgr.ensure_workspace(
        repo="octo/widget",
        number=43,
        title="something is wrong",
        clone_url=str(upstream_repo),
        default_branch="main",
        slot_uid=2001,
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )

    assert calls == [(ws.root, 2001)]


def test_ensure_workspace_provisions_and_slot_owns_runtime_dirs(
    tmp_path: Path, upstream_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned: dict[Path, tuple[int, int]] = {}
    runtime_paths: list[Path] = []
    real_chown = _chown_workspace

    def record_chown(ws_root: Path, slot_uid: int | None) -> None:
        assert slot_uid is not None
        paths = [
            ws_root / ".omp-tmp",
            ws_root / ".omp-xdg" / "data",
            ws_root / ".omp-xdg" / "data" / "omp",
            ws_root / ".omp-xdg" / "state",
            ws_root / ".omp-xdg" / "state" / "omp",
            ws_root / ".omp-xdg" / "cache",
            ws_root / ".omp-xdg" / "cache" / "omp",
            ws_root / ".omp-xdg" / "cache" / "bun-install",
        ]
        runtime_paths.extend(paths)
        for path in paths:
            assert path.is_dir()
            owned[path] = (slot_uid, slot_uid)
        # Same rationale as test_ensure_workspace_invokes_slot_chown: hand
        # the tree to the slot so the subsequent `git config` works under
        # real slot permissions in CI.
        real_chown(ws_root, slot_uid)

    monkeypatch.setattr("robomp.sandbox._chown_workspace", record_chown)
    mgr = SandboxManager(tmp_path / "workspaces")

    ws = mgr.ensure_workspace(
        repo="octo/widget",
        number=46,
        title="runtime perms",
        clone_url=str(upstream_repo),
        default_branch="main",
        slot_uid=2001,
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )

    assert runtime_paths
    assert set(runtime_paths) == {
        ws.root / ".omp-tmp",
        ws.root / ".omp-xdg" / "data",
        ws.root / ".omp-xdg" / "data" / "omp",
        ws.root / ".omp-xdg" / "state",
        ws.root / ".omp-xdg" / "state" / "omp",
        ws.root / ".omp-xdg" / "cache",
        ws.root / ".omp-xdg" / "cache" / "omp",
        ws.root / ".omp-xdg" / "cache" / "bun-install",
    }
    assert set(owned.values()) == {(2001, 2001)}


def test_ensure_workspace_is_idempotent(tmp_path: Path, upstream_repo: Path) -> None:
    mgr = SandboxManager(tmp_path / "workspaces")
    ws1 = mgr.ensure_workspace(
        repo="octo/widget",
        number=5,
        title="t",
        clone_url=str(upstream_repo),
        default_branch="main",
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    ws2 = mgr.ensure_workspace(
        repo="octo/widget",
        number=5,
        title="t",
        clone_url=str(upstream_repo),
        default_branch="main",
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    assert ws1.repo_dir == ws2.repo_dir
    assert ws1.branch == ws2.branch


def test_ensure_workspace_existing_branch_starts_from_remote_head(tmp_path: Path, upstream_repo: Path) -> None:
    branch = "farm/abc12345/existing-pr"
    seed = tmp_path / "remote-branch-seed"
    _git(["clone", str(upstream_repo), str(seed)], cwd=tmp_path)
    _git(["-C", str(seed), "checkout", "-b", branch], cwd=tmp_path)
    (seed / "README.md").write_text("from pr branch\n", encoding="utf-8")
    _git(["-C", str(seed), "add", "README.md"], cwd=tmp_path)
    subprocess.run(
        ["git", "commit", "-m", "pr branch"],
        cwd=str(seed),
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
    _git(["-C", str(seed), "push", "origin", branch], cwd=tmp_path)

    mgr = SandboxManager(tmp_path / "workspaces")
    ws = mgr.ensure_workspace(
        repo="octo/widget",
        number=77,
        title="follow up",
        clone_url=str(upstream_repo),
        default_branch="main",
        existing_branch=branch,
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )

    assert ws.branch == branch
    assert (ws.repo_dir / "README.md").read_text(encoding="utf-8") == "from pr branch\n"


def test_remove_workspace(tmp_path: Path, upstream_repo: Path) -> None:
    mgr = SandboxManager(tmp_path / "workspaces")
    ws = mgr.ensure_workspace(
        repo="octo/widget",
        number=12,
        title="t",
        clone_url=str(upstream_repo),
        default_branch="main",
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    assert ws.repo_dir.exists()
    mgr.remove_workspace(repo="octo/widget", number=12)
    assert not ws.repo_dir.exists()
    assert not ws.root.exists()


def test_redact_credentials_strips_userinfo() -> None:
    from robomp.sandbox import redact_credentials

    assert (
        redact_credentials("Cloning into 'x' from https://bot:ghp_secret@github.com/o/r.git failed")
        == "Cloning into 'x' from https://***@github.com/o/r.git failed"
    )
    # Multiple URLs in one string.
    assert (
        redact_credentials("a https://x:y@example.com b https://q:z@example.org c")
        == "a https://***@example.com b https://***@example.org c"
    )
    # No-op on strings without credentials.
    assert redact_credentials("plain message") == "plain message"
    assert redact_credentials(None) == ""


def test_git_command_error_redacts_url_in_args_and_stderr(tmp_path: Path) -> None:
    """An ENOENT-style git failure on a credentialed clone URL must not echo the token."""
    import pytest as _pytest

    from robomp.sandbox import _run

    cred_url = "https://bot:ghp_abc123secret@example.invalid/o/r.git"
    with _pytest.raises(Exception) as exc:
        _run(["git", "clone", cred_url, str(tmp_path / "out")])
    text = str(exc.value)
    assert "ghp_abc123secret" not in text
    assert "bot" not in text or "https://bot:" not in text
    assert "***" in text or "example.invalid" in text


def test_ensure_workspace_rewrites_credentialed_origin(tmp_path: Path, upstream_repo: Path) -> None:
    """A pool clone created by an older deploy with `https://user:pass@…` in
    `.git/config` must have its `origin` URL rewritten to the credential-free
    URL before the next fetch — credentials NEVER persist on disk."""
    mgr = SandboxManager(tmp_path / "workspaces")
    # Pre-seed the pool by hand, simulating an older deploy: clone, then
    # rewrite `origin` to a credentialed URL pointing at the same local bare.
    pool = mgr.pool_path("octo/widget")
    pool.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", "--filter=blob:none", str(upstream_repo), str(pool)], cwd=tmp_path)
    credentialed = "https://bot:ghp_seekrit@example.invalid/octo/widget.git"
    _git(["-C", str(pool), "remote", "set-url", "origin", credentialed], cwd=tmp_path)
    config = (pool / ".git" / "config").read_text()
    assert "ghp_seekrit" in config  # sanity: precondition

    # Now resolve through ensure_workspace using the clean URL we now own.
    # The fetch step itself will fail against the bogus example.invalid host,
    # so route through a clean local URL by setting it as the canonical
    # clone_url; the remote MUST be rewritten BEFORE fetch.
    mgr.ensure_workspace(
        repo="octo/widget",
        number=7,
        title="t",
        clone_url=str(upstream_repo),
        default_branch="main",
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    config_after = (pool / ".git" / "config").read_text()
    assert "ghp_seekrit" not in config_after, config_after
    assert "bot:" not in config_after, config_after
    # Origin now points at the clean URL.
    url = subprocess.run(
        ["git", "-C", str(pool), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert url == str(upstream_repo)


def test_push_force_with_lease_succeeds_after_local_amend(tmp_path: Path, upstream_repo: Path) -> None:
    """An agent amending an already-pushed commit (e.g. `--reset-author`) must
    still be able to push: `--force-with-lease` allows the local rewrite as
    long as origin still matches what we last fetched."""
    from robomp.git_ops import push as git_push

    # Clone, make a commit, push, then amend and push again.
    work = tmp_path / "work"
    _git(["clone", str(upstream_repo), str(work)], cwd=tmp_path)
    _git(["-C", str(work), "config", "user.email", "t@t"], cwd=tmp_path)
    _git(["-C", str(work), "config", "user.name", "t"], cwd=tmp_path)
    _git(["-C", str(work), "checkout", "-b", "farm/abc/topic"], cwd=tmp_path)
    (work / "x.txt").write_text("a\n")
    _git(["-C", str(work), "add", "x.txt"], cwd=tmp_path)
    _git(["-C", str(work), "commit", "-m", "initial"], cwd=tmp_path)
    git_push(work, branch="farm/abc/topic", expected_head=None, token=None)

    # Amend (rewrites the SHA at origin/farm/abc/topic).
    (work / "x.txt").write_text("a-amended\n")
    _git(["-C", str(work), "add", "x.txt"], cwd=tmp_path)
    _git(["-C", str(work), "commit", "--amend", "--no-edit"], cwd=tmp_path)
    amended = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    result = git_push(work, branch="farm/abc/topic", expected_head=None, token=None)
    assert result.head == amended

    # Origin's branch ref now matches the amended SHA.
    on_origin = subprocess.run(
        ["git", "-C", str(upstream_repo), "rev-parse", "refs/heads/farm/abc/topic"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert on_origin == amended


def test_push_force_with_lease_refuses_when_origin_moved(tmp_path: Path, upstream_repo: Path) -> None:
    """If origin's branch ref has been moved by some other writer between our
    last fetch and this push, the lease MUST refuse — even though we're
    force-pushing."""
    from robomp.git_ops import GitCommandError
    from robomp.git_ops import push as git_push

    work = tmp_path / "work"
    _git(["clone", str(upstream_repo), str(work)], cwd=tmp_path)
    _git(["-C", str(work), "config", "user.email", "t@t"], cwd=tmp_path)
    _git(["-C", str(work), "config", "user.name", "t"], cwd=tmp_path)
    _git(["-C", str(work), "checkout", "-b", "farm/abc/topic"], cwd=tmp_path)
    (work / "x.txt").write_text("a\n")
    _git(["-C", str(work), "add", "x.txt"], cwd=tmp_path)
    _git(["-C", str(work), "commit", "-m", "initial"], cwd=tmp_path)
    git_push(work, branch="farm/abc/topic", expected_head=None, token=None)

    # A "sneaky" second writer publishes a different SHA to the same ref on
    # origin — pushed from an independent worktree, NOT seen by `work`'s
    # remote-tracking ref.
    intruder = tmp_path / "intruder"
    _git(["clone", str(upstream_repo), str(intruder)], cwd=tmp_path)
    _git(["-C", str(intruder), "config", "user.email", "i@i"], cwd=tmp_path)
    _git(["-C", str(intruder), "config", "user.name", "i"], cwd=tmp_path)
    _git(["-C", str(intruder), "checkout", "-b", "farm/abc/topic", "origin/farm/abc/topic"], cwd=tmp_path)
    (intruder / "x.txt").write_text("from-intruder\n")
    _git(["-C", str(intruder), "add", "x.txt"], cwd=tmp_path)
    _git(["-C", str(intruder), "commit", "--amend", "--no-edit"], cwd=tmp_path)
    _git(["-C", str(intruder), "push", "--force", "origin", "farm/abc/topic"], cwd=tmp_path)

    # Now `work` tries to push another amended commit. The lease pins the
    # expected origin SHA to whatever `work`'s remote-tracking ref still
    # records — which is now stale — so origin's actual SHA differs and the
    # push must be refused.
    (work / "x.txt").write_text("from-us\n")
    _git(["-C", str(work), "add", "x.txt"], cwd=tmp_path)
    _git(["-C", str(work), "commit", "--amend", "--no-edit"], cwd=tmp_path)
    with pytest.raises(GitCommandError) as exc:
        git_push(work, branch="farm/abc/topic", expected_head=None, token=None)
    assert (
        "stale info" in (exc.value.stderr + exc.value.stdout).lower()
        or "rejected" in (exc.value.stderr + exc.value.stdout).lower()
    )


def test_run_git_injects_safe_directory_and_subprocess_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from robomp.git_ops import _run_git

    monkeypatch.setenv("GIT_TRACE", "1")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/askpass")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o ProxyCommand=bad")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    captured: dict[str, object] = {}

    def fake_run_process(
        cmd: list[str],
        *,
        cwd: Path | None,
        env: dict[str, str],
        timeout: float | None,
        stdin: str | None = None,
        user: int | None = None,
        group: int | None = None,
        extra_groups: list[int] | tuple[int, ...] | None = None,
        umask: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        captured.update(
            {
                "cmd": cmd,
                "cwd": cwd,
                "env": env,
                "timeout": timeout,
                "stdin": stdin,
                "user": user,
                "group": group,
                "extra_groups": extra_groups,
                "umask": umask,
            }
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("robomp.git_ops._run_process", fake_run_process)

    _run_git(
        ["status"],
        cwd=tmp_path,
        token=None,
        safe_directory=Path("/x"),
        user=2001,
        group=2001,
        extra_groups=[2000],
        umask=0o002,
    )

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_0"] == "/x"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_TRACE" not in env
    assert "GIT_ASKPASS" not in env
    assert "GIT_SSH_COMMAND" not in env
    assert "OPENAI_API_KEY" not in env
    assert captured["user"] == 2001
    assert captured["group"] == 2001
    assert captured["extra_groups"] == [2000]
    assert captured["umask"] == 0o002


def test_run_git_kills_hung_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `git` invocation that hangs past the timeout must be killed and
    raised as `GitCommandError(124)` rather than pinning the calling
    thread. The proxy already bounds the async caller via
    `asyncio.wait_for`, but the OS process only goes away because of this
    timeout."""
    from robomp.git_ops import GitCommandError, _run_git

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake_git = fakebin / "git"
    # Use `exec /bin/sleep 30` so the kill from `subprocess.run`'s timeout
    # actually terminates the wait — `sh` with a non-exec `sleep` would
    # keep the parent alive on SIGTERM, and the absolute path means the
    # shim doesn't depend on PATH (we point PATH at fakebin so `git`
    # itself resolves to our shim).
    fake_git.write_text("#!/bin/sh\nexec /bin/sleep 30\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", str(fakebin))

    with pytest.raises(GitCommandError) as exc:
        _run_git(["status"], cwd=tmp_path, token=None, timeout=0.5)
    assert exc.value.returncode == 124
    assert "timed out" in exc.value.stderr.lower()


# ---------------------------------------------------------------------------
# Partial-clone blob backfill (oh-my-pi#1818)
# ---------------------------------------------------------------------------


def _partial_clone_upstream(tmp_path: Path) -> Path:
    """Bare upstream that advertises ``uploadpack.allowFilter`` so partial
    clones over ``file://`` actually skip blobs (local-protocol clones
    otherwise ignore ``--filter``)."""
    repo = tmp_path / "partial-upstream.git"
    repo.mkdir()
    _git(["init", "--initial-branch=main", "--bare", str(repo)], cwd=tmp_path)
    _git(["-C", str(repo), "config", "uploadpack.allowFilter", "true"], cwd=tmp_path)
    _git(["-C", str(repo), "config", "uploadpack.allowAnySHA1InWant", "true"], cwd=tmp_path)
    seed = tmp_path / "partial-seed"
    seed.mkdir()
    _git(["init", "--initial-branch=main", str(seed)], cwd=tmp_path)
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["-C", str(seed), "add", "."], cwd=tmp_path)
    subprocess.run(
        ["git", "-C", str(seed), "commit", "-m", "init"],
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
    _git(["-C", str(seed), "remote", "add", "origin", str(repo)], cwd=tmp_path)
    _git(["-C", str(seed), "push", "origin", "main"], cwd=tmp_path)
    return repo


def _commit_new_blob_upstream(upstream: Path, tmp_path: Path, *, path: str, content: str, ref: str = "main") -> str:
    """Add a fresh blob upstream and return the new commit SHA."""
    contrib = tmp_path / f"contrib-{path.replace('/', '_')}-{ref.replace('/', '_')}"
    _git(["clone", f"file://{upstream}", str(contrib)], cwd=tmp_path)
    (contrib / path).parent.mkdir(parents=True, exist_ok=True)
    (contrib / path).write_text(content, encoding="utf-8")
    _git(["-C", str(contrib), "add", path], cwd=tmp_path)
    subprocess.run(
        ["git", "-C", str(contrib), "commit", "-m", f"add {path}"],
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
    sha = subprocess.run(
        ["git", "-C", str(contrib), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(["-C", str(contrib), "push", "origin", f"HEAD:{ref}"], cwd=tmp_path)
    return sha


def _missing_object_oids(repo: Path, rev: str) -> list[str]:
    """OIDs of promisor-deferred objects reachable from ``rev``."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--objects", "--missing=print", rev],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line[1:].split()[0] for line in proc.stdout.splitlines() if line.startswith("?")]


def test_fetch_ref_backfills_missing_blobs_into_partial_clone(tmp_path: Path) -> None:
    """Regression for oh-my-pi#1818: ``fetch_ref`` is called immediately before
    ``git worktree add origin/<ref>``. On a ``--filter=blob:none`` pool whose
    periodic ``fetch --prune`` inherited that filter, the ref's blobs are
    absent and the worktree-add triggers a promisor lazy fetch that — under
    proxy transport — has no PAT and dies. ``fetch_ref`` MUST materialize
    every reachable blob so the checkout never hits the lazy path."""
    upstream = _partial_clone_upstream(tmp_path)
    pool = tmp_path / "pool"
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
        cwd=tmp_path,
    )

    # New upstream commit → fresh blob not yet pulled into the pool.
    _commit_new_blob_upstream(upstream, tmp_path, path="payload.txt", content="v2 contents here\n")

    # Pool refresh mirrors `SandboxManager.ensure_clone` → inherits filter.
    git_fetch_prune(pool, token=None)
    missing_before = _missing_object_oids(pool, "origin/main")
    assert missing_before, (
        "test precondition broken: partial-clone fetch should leave at least one blob promisor-deferred"
    )

    # Pool config must show the partial-clone state we're recovering from.
    cfg_before = (pool / ".git" / "config").read_text(encoding="utf-8")
    assert "partialclonefilter = blob:none" in cfg_before
    assert "promisor = true" in cfg_before

    # The fix: fetch_ref backfills every reachable blob in a single call.
    git_fetch_ref(pool, "main", token=None)

    missing_after = _missing_object_oids(pool, "origin/main")
    assert missing_after == [], f"fetch_ref left missing objects: {missing_after}"

    # And the partial-clone config is intact — `fetch_prune` stays cheap on
    # the next pool refresh; only the explicit pre-checkout fetch eagerly
    # fills blobs.
    cfg_after = (pool / ".git" / "config").read_text(encoding="utf-8")
    assert "partialclonefilter = blob:none" in cfg_after
    assert "promisor = true" in cfg_after

    # End-to-end: worktree add must succeed even if origin is unreachable —
    # the blobs are local now, no lazy fetch can fire.
    _git(["-C", str(pool), "remote", "set-url", "origin", "https://example.invalid/missing.git"], cwd=tmp_path)
    ws_dir = tmp_path / "ws"
    subprocess.run(
        ["git", "-C", str(pool), "worktree", "add", "-b", "verify-1818", str(ws_dir), "origin/main"],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ | {"GIT_TERMINAL_PROMPT": "0"},
    )
    assert (ws_dir / "payload.txt").read_text(encoding="utf-8") == "v2 contents here\n"


def test_fetch_pr_head_backfills_missing_blobs_into_partial_clone(tmp_path: Path) -> None:
    """Same regression as ``test_fetch_ref_backfills…`` but on the PR-review
    path: ``fetch_pr_head`` precedes ``git worktree add --detach FETCH_HEAD``
    and so MUST eagerly fetch blobs reachable from the PR head."""
    upstream = _partial_clone_upstream(tmp_path)
    pool = tmp_path / "pool"
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
        cwd=tmp_path,
    )

    # Publish a PR head with a fresh blob.
    pr_sha = _commit_new_blob_upstream(
        upstream, tmp_path, path="pr.txt", content="pr blob payload\n", ref="refs/pull/7/head"
    )

    git_fetch_pr_head(pool, 7, token=None)
    missing_after = _missing_object_oids(pool, pr_sha)
    assert missing_after == [], f"fetch_pr_head left missing objects: {missing_after}"

    # End-to-end: a detached worktree add against the freshly-fetched PR head
    # succeeds without lazy-fetching against (now-broken) origin.
    _git(["-C", str(pool), "remote", "set-url", "origin", "https://example.invalid/missing.git"], cwd=tmp_path)
    ws_dir = tmp_path / "pr-ws"
    subprocess.run(
        ["git", "-C", str(pool), "worktree", "add", "--detach", str(ws_dir), "FETCH_HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ | {"GIT_TERMINAL_PROMPT": "0"},
    )
    assert (ws_dir / "pr.txt").read_text(encoding="utf-8") == "pr blob payload\n"


def test_normalize_pr_sparse_paths_adds_hoa_tooling_support() -> None:
    paths = normalize_pr_sparse_paths(
        (
            "apps/hoa/api/admin_api/viewsets/charges.py",
            "apps/hoa/hoa-web/apps/admin/src/routes/specific/charge/list.tsx",
        )
    )

    assert "apps/hoa/api/admin_api/viewsets/charges.py" in paths
    assert "apps/hoa/hoa-web/apps/admin/src/routes/specific/charge/list.tsx" in paths
    assert "apps/hoa/manage.py" in paths
    assert "apps/hoa/pyproject.toml" in paths
    assert "apps/hoa/uv.lock" in paths
    assert "apps/hoa/hoa-web/package.json" in paths
    assert "apps/hoa/hoa-web/yarn.lock" in paths
    assert "apps/hoa/hoa-web/apps/admin/package.json" in paths


def test_prepare_pr_worktree_sparse_hydrates_changed_paths_only(tmp_path: Path) -> None:
    upstream = _partial_clone_upstream(tmp_path)
    pool = tmp_path / "pool"
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
        cwd=tmp_path,
    )
    _commit_new_blob_upstream(upstream, tmp_path, path="src/changed.txt", content="base payload\n")
    _commit_new_blob_upstream(upstream, tmp_path, path="docs/untouched.txt", content="untouched payload\n")

    pr_seed = tmp_path / "pr-seed"
    _git(["clone", f"file://{upstream}", str(pr_seed)], cwd=tmp_path)
    (pr_seed / "src" / "changed.txt").write_text("changed payload\n", encoding="utf-8")
    _git(["-C", str(pr_seed), "add", "src/changed.txt"], cwd=tmp_path)
    subprocess.run(
        ["git", "-C", str(pr_seed), "commit", "-m", "modify pr file"],
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
    pr_sha = subprocess.run(
        ["git", "-C", str(pr_seed), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(["-C", str(pr_seed), "push", "origin", "HEAD:refs/pull/7/head"], cwd=tmp_path)

    ws_dir = tmp_path / "pr-sparse-ws"
    result = git_prepare_pr_worktree(
        pool,
        ws_dir,
        pr_number=7,
        expected_head_sha=pr_sha,
        base_ref="main",
        changed_paths=("src/changed.txt",),
        token=None,
    )

    assert result.hydrated_paths == ("src/changed.txt",)
    assert (ws_dir / "src/changed.txt").read_text(encoding="utf-8") == "changed payload\n"
    assert not (ws_dir / "docs/untouched.txt").exists()

    _git(["-C", str(pool), "remote", "set-url", "origin", "https://example.invalid/missing.git"], cwd=tmp_path)
    diff_proc = subprocess.run(
        ["git", "-C", str(ws_dir), "diff", "--no-color", "origin/main...HEAD", "--", "src/changed.txt"],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ | {"GIT_TERMINAL_PROMPT": "0"},
    )
    assert "-base payload" in diff_proc.stdout
    assert "+changed payload" in diff_proc.stdout


def test_pr_sparse_checkout_patterns_backend_only_excludes_frontend() -> None:
    patterns = pr_sparse_checkout_patterns(("apps/hoa/api/tests/actions/test_x.py",))
    assert patterns == ("/apps/hoa/", "!/apps/hoa/hoa-web/")


def test_pr_sparse_checkout_patterns_frontend_only() -> None:
    patterns = pr_sparse_checkout_patterns(
        ("apps/hoa/hoa-web/apps/admin/src/routes/specific/charge/list.tsx",)
    )
    assert patterns == ("/apps/hoa/hoa-web/",)


def test_pr_sparse_checkout_patterns_backend_and_frontend_hydrate_whole_root() -> None:
    patterns = pr_sparse_checkout_patterns(
        (
            "apps/hoa/api/models.py",
            "apps/hoa/hoa-web/apps/admin/src/list.tsx",
        )
    )
    # A change spanning both projects hydrates all of apps/hoa with no exclusion.
    assert patterns == ("/apps/hoa/",)


def test_pr_sparse_checkout_patterns_non_hoa_stays_file_scoped() -> None:
    assert pr_sparse_checkout_patterns((".github/workflows/ci.yml",)) == (
        ".github/workflows/ci.yml",
    )


def test_pr_sparse_checkout_patterns_accepts_single_use_iterator() -> None:
    # `prepare_pr_worktree` materializes the iterable, but the function itself
    # must also tolerate a one-shot iterator without silently dropping paths.
    patterns = pr_sparse_checkout_patterns(iter(("apps/hoa/api/x.py",)))
    assert patterns == ("/apps/hoa/", "!/apps/hoa/hoa-web/")


def test_pr_sparse_checkout_patterns_bare_frontend_root_hydrates_frontend() -> None:
    # A path equal to the bare root (no trailing slash) must not be misread as a
    # backend-only change that excludes the frontend workspace.
    assert pr_sparse_checkout_patterns(("apps/hoa/hoa-web",)) == ("/apps/hoa/hoa-web/",)


def test_pr_sparse_checkout_patterns_mixes_hoa_root_and_other_files() -> None:
    patterns = pr_sparse_checkout_patterns(
        ("apps/hoa/api/x.py", ".github/workflows/ci.yml")
    )
    assert patterns[0] == "/apps/hoa/"
    assert "!/apps/hoa/hoa-web/" in patterns
    assert ".github/workflows/ci.yml" in patterns
    # HOA backend support files (manage.py/pyproject/uv.lock) added by
    # normalize_pr_sparse_paths must not leak out as standalone file patterns —
    # they are already covered by the hydrated `/apps/hoa/` root.
    assert "apps/hoa/manage.py" not in patterns


def test_prepare_pr_worktree_hydrates_django_project_excluding_frontend(tmp_path: Path) -> None:
    """A backend change hydrates the whole Django project (so `hoa.settings`
    imports resolve for `manage.py test`) but leaves the heavy `hoa-web`
    frontend workspace out of the checkout."""
    upstream = _partial_clone_upstream(tmp_path)
    pool = tmp_path / "pool"
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
        cwd=tmp_path,
    )
    # Base contains a full apps/hoa project plus a hoa-web frontend file.
    _commit_new_blob_upstream(upstream, tmp_path, path="apps/hoa/hoa/settings.py", content="SETTINGS = 1\n")
    _commit_new_blob_upstream(upstream, tmp_path, path="apps/hoa/manage.py", content="# manage\n")
    _commit_new_blob_upstream(upstream, tmp_path, path="apps/hoa/api/models.py", content="MODELS = 0\n")
    _commit_new_blob_upstream(upstream, tmp_path, path="apps/hoa/hoa-web/package.json", content="{}\n")

    pr_seed = tmp_path / "pr-seed"
    _git(["clone", f"file://{upstream}", str(pr_seed)], cwd=tmp_path)
    (pr_seed / "apps/hoa/api/models.py").write_text("MODELS = 1\n", encoding="utf-8")
    _git(["-C", str(pr_seed), "add", "apps/hoa/api/models.py"], cwd=tmp_path)
    subprocess.run(
        ["git", "-C", str(pr_seed), "commit", "-m", "modify api"],
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
    pr_sha = subprocess.run(
        ["git", "-C", str(pr_seed), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(["-C", str(pr_seed), "push", "origin", "HEAD:refs/pull/9/head"], cwd=tmp_path)

    ws_dir = tmp_path / "pr-hoa-ws"
    result = git_prepare_pr_worktree(
        pool,
        ws_dir,
        pr_number=9,
        expected_head_sha=pr_sha,
        base_ref="main",
        # A single-use iterator must survive both internal normalizers.
        changed_paths=iter(("apps/hoa/api/models.py",)),
        token=None,
    )

    assert result.hydrated_paths == ("/apps/hoa/", "!/apps/hoa/hoa-web/")
    assert (ws_dir / "apps/hoa/api/models.py").read_text(encoding="utf-8") == "MODELS = 1\n"
    # Django settings package present → `manage.py test` can import `hoa.settings`.
    assert (ws_dir / "apps/hoa/hoa/settings.py").exists()
    assert (ws_dir / "apps/hoa/manage.py").exists()
    # Heavy frontend workspace stays out of the checkout.
    assert not (ws_dir / "apps/hoa/hoa-web/package.json").exists()


@pytest.mark.parametrize("bad_path", ["../x", "/x", ".git/config", "dir/.git/config", "", "a\0b"])
def test_prepare_pr_worktree_rejects_unsafe_sparse_paths(tmp_path: Path, bad_path: str) -> None:
    upstream = _partial_clone_upstream(tmp_path)
    pool = tmp_path / "pool"
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
        cwd=tmp_path,
    )

    with pytest.raises(ValueError):
        git_prepare_pr_worktree(
            pool,
            tmp_path / "unsafe-ws",
            pr_number=7,
            expected_head_sha="0" * 40,
            base_ref="main",
            changed_paths=(bad_path,),
            token=None,
        )


def test_prepare_pr_worktree_rejects_unexpected_head(tmp_path: Path) -> None:
    upstream = _partial_clone_upstream(tmp_path)
    pool = tmp_path / "pool"
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
        cwd=tmp_path,
    )
    _commit_new_blob_upstream(upstream, tmp_path, path="src/changed.txt", content="base payload\n")
    _commit_new_blob_upstream(upstream, tmp_path, path="src/changed.txt", content="changed payload\n", ref="refs/pull/7/head")

    with pytest.raises(GitCommandError, match="PR head mismatch"):
        git_prepare_pr_worktree(
            pool,
            tmp_path / "mismatch-ws",
            pr_number=7,
            expected_head_sha="0" * 40,
            base_ref="main",
            changed_paths=("src/changed.txt",),
            token=None,
        )

# ---------------------------------------------------------------------------
# NativesCache integration into ensure_workspace
# ---------------------------------------------------------------------------


def _seed_native_dir(repo_dir: Path) -> Path:
    native_dir = repo_dir / "packages" / "natives" / "native"
    native_dir.mkdir(parents=True, exist_ok=True)
    return native_dir


def test_ensure_workspace_without_cache_leaves_native_dir_untouched(tmp_path: Path, upstream_repo: Path) -> None:
    mgr = SandboxManager(tmp_path / "workspaces")
    ws = mgr.ensure_workspace(
        repo="octo/widget",
        number=10,
        title="no cache",
        clone_url=str(upstream_repo),
        default_branch="main",
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    assert mgr.natives_cache is None
    # No `packages/natives/native/` was tracked in the upstream, and no cache
    # is configured → the directory wasn't created by populate.
    assert not (ws.repo_dir / "packages" / "natives" / "native").exists()


def test_ensure_workspace_populates_from_natives_cache(tmp_path: Path, upstream_repo: Path) -> None:
    from robomp.natives_cache import NativesCache, compute_key, target_triple

    cache = NativesCache(tmp_path / "natives-cache")
    mgr = SandboxManager(tmp_path / "workspaces", natives_cache=cache)

    # First workspace: stage built artifacts, capture under the workspace's key.
    ws1 = mgr.ensure_workspace(
        repo="octo/widget",
        number=11,
        title="producer",
        clone_url=str(upstream_repo),
        default_branch="main",
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    native_dir1 = _seed_native_dir(ws1.repo_dir)
    # Mirror the napi build output set. The filename must match the live
    # `target_triple()` value or the populate path won't recognize it.
    triple = target_triple()
    (native_dir1 / f"pi_natives.{triple}.node").write_bytes(b"ELFx")
    (native_dir1 / "index.d.ts").write_text("export const X: number;\n")
    (native_dir1 / "index.js").write_text("export const X = 1;\n")
    (native_dir1 / "embedded-addon.js").write_text("export const embeddedAddon = null;\n")
    key = compute_key(ws1.repo_dir)  # default target = target_triple()
    assert cache.capture("octo/widget", key, native_dir1) is not None

    # Second workspace on the same source HEAD: ensure_workspace auto-populates.
    # We force the same key by pinning TARGET_VARIANT (only relevant on x64;
    # harmless on arm64) — actually compute_key uses target_triple() at call
    # time. To make the test platform-independent, override populate to use
    # the same key explicitly.
    ws2 = mgr.ensure_workspace(
        repo="octo/widget",
        number=12,
        title="consumer",
        clone_url=str(upstream_repo),
        default_branch="main",
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    native_dir2 = ws2.repo_dir / "packages" / "natives" / "native"
    # The auto-populate path used the real target_triple() — which matches
    # the host that just captured. So the same key applies and files appear.
    assert native_dir2.is_dir(), "populate should have created native/ on hit"
    node_name = f"pi_natives.{triple}.node"
    assert (native_dir2 / node_name).read_bytes() == b"ELFx"
    # The .node is hardlinked, sharing the cache's inode.
    cached_node = cache.entry_dir("octo/widget", key) / node_name
    ws2_node = native_dir2 / node_name
    assert cached_node.stat().st_ino == ws2_node.stat().st_ino


def test_ensure_workspace_cache_miss_is_silent_noop(tmp_path: Path, upstream_repo: Path) -> None:
    from robomp.natives_cache import NativesCache

    cache = NativesCache(tmp_path / "empty-cache")
    mgr = SandboxManager(tmp_path / "workspaces", natives_cache=cache)
    ws = mgr.ensure_workspace(
        repo="octo/widget",
        number=13,
        title="miss",
        clone_url=str(upstream_repo),
        default_branch="main",
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    # Cache is empty so the workspace ends up identical to the no-cache case.
    assert ws.repo_dir.is_dir()
    assert not (ws.repo_dir / "packages" / "natives" / "native").exists()
