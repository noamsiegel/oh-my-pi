"""Per-issue workspace lifecycle: clone pool + git worktrees.

The remote-facing git operations (clone, fetch, push) go through a pluggable
`GitTransport` so a deploy can keep the PAT entirely in a separate `gh-proxy`
container. The default `LocalGitTransport` runs git in-process with ephemeral
PAT injection via `--config-env` (see `robomp.git_ops`); the `ProxyGitTransport`
in `robomp.proxy_client` forwards the same set of operations over HMAC RPC.

Per-issue worktree add/remove stays local — those operations only touch the
shared on-disk pool clone, no remote authentication required.

Permission model
----------------
There are four ownership zones on disk; do not let them blur:

1. **Workspace tree** (`/data/workspaces/<key>/`, including `repo/`,
   `.omp-session/`, `context/`, `artifacts/`, `.omp-tmp/`, `.omp-xdg`):
   single-owner. Owned by the active slot UID/GID (`omp-N`) when slot
   isolation is enabled, otherwise by the orchestrator's own UID/GID. Modes
   stay `u=rwX,g=rwX,o=` (effectively `0770` dirs / `0660` files). The
   orchestrator (root) reads/writes via uid-0 bypass when it must, and drops
   to the slot for any subprocess that touches paths the agent will revisit.
   `ensure_workspace` + `_chown_workspace` are the single point of truth for
   this zone — no other helper sets ownership inside `ws_root`.
2. **Clone pool** (`/data/workspaces/_pool/<owner>__<repo>/`): genuinely
   multi-slot. Owned by `root:omp` (gid 2000) with setgid `02770`; cross-slot
   writes are bridged by `_share_git_metadata_with_slots`.
3. **Language tool caches** (`/data/cache/{cargo,cargo-target,rustup,bun-cache}`):
   multi-slot. Owned by `root:omp` with setgid `02770`; provisioned by
   `entrypoint.sh`.
4. **Agent HOME template** (`/srv/agent-home`): read-only, `root:root`
   `0755/0644`.

Bun's install cache stays workspace-private (zone 1) on purpose — bun
chmod/utimes its own cache root, which breaks any shared-cache scheme.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import secrets
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Protocol

from robomp.git_ops import (
    GitCommandError,
    PrWorktreeResult,
    PushResult,
    clone as git_clone,
    fetch_pr_head as git_fetch_pr_head,
    fetch_prune as git_fetch_prune,
    fetch_ref as git_fetch_ref,
    prepare_pr_worktree as git_prepare_pr_worktree,
    pr_sparse_checkout_patterns,
    redact_credentials,
    push as git_push,
)
from robomp.natives_cache import CacheHit, NativesCache
from robomp.natives_cache import compute_key as natives_compute_key

log = logging.getLogger(__name__)
_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 120.0




@dataclass(slots=True)
class Workspace:
    """Resolved per-issue scratch space."""

    root: Path
    repo_dir: Path
    session_dir: Path
    context_dir: Path
    artifacts_dir: Path
    branch: str
    repo_full_name: str
    issue_number: int
    review_head_sha: str | None = None
    @property
    def repro_dir(self) -> Path:
        return self.context_dir / "repro"

    @property
    def workspace_key(self) -> str:
        if self.review_head_sha:
            return pr_review_workspace_key(self.repo_full_name, self.issue_number, self.review_head_sha)
        return workspace_key(self.repo_full_name, self.issue_number)


@dataclass(slots=True)
class WorkspaceGcEntry:
    """Parsed metadata for one candidate workspace directory."""

    root: Path
    repo: str
    number: int
    head_sha: str | None
    mtime: float
    size_bytes: int

    @property
    def issue_key(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass(slots=True)
class WorkspaceGcResult:
    """Outcome of a single ``gc_workspaces`` sweep."""

    evicted: int
    freed_bytes: int
    total_bytes: int
    free_bytes: int
    remaining_bytes: int
    scanned: int


@dataclass(slots=True)
class WorkspaceStorageStats:
    """Snapshot of workspace disk consumption for metrics."""

    entries: int
    bytes: int
    free_bytes: int
    total_bytes: int


_HEX40_RE = re.compile(r"[0-9a-fA-F]{40}")


def _dir_size_bytes(path: Path) -> int:
    """Sum on-disk byte size of ``path`` without following symlink targets."""
    total = 0
    for current_root, dirs, files in os.walk(path, topdown=True, followlinks=False):
        current = Path(current_root)
        for name in files:
            try:
                total += (current / name).lstat().st_size
            except OSError:
                continue
        for name in dirs:
            try:
                st = (current / name).lstat()
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                total += st.st_size
    return total


def _parse_workspace_dir_name(name: str) -> tuple[str, int, str | None] | None:
    """Fallback parse of a workspace directory name into (repo, number, head_sha).

    Recognizes ``<repo_key>__<number>__<40hex>`` (PR review) and
    ``<repo_key>__<number>`` (legacy issue). Returns ``None`` for malformed
    names so GC never deletes directories it cannot identify.
    """
    parts = name.split("__")
    if len(parts) < 2:
        return None
    last = parts[-1]
    if _HEX40_RE.fullmatch(last):
        if len(parts) < 3:
            return None
        number_str = parts[-2]
        repo_key = "__".join(parts[:-2])
        head_sha: str | None = last.lower()
    else:
        number_str = last
        repo_key = "__".join(parts[:-1])
        head_sha = None
    if not repo_key or not number_str.isdigit():
        return None
    return repo_key.replace("__", "/"), int(number_str), head_sha


def _slug(text: str, *, length: int = 40) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not cleaned:
        cleaned = "issue"
    return cleaned[:length]


def _short_hex(seed: str | None = None) -> str:
    if seed:
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    return secrets.token_hex(4)


def workspace_key(repo: str, number: int) -> str:
    return f"{repo.replace('/', '__')}__{number}"

def _validate_pr_head_sha(head_sha: str) -> str:
    if re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
        return head_sha.lower()
    raise ValueError(f"invalid PR head sha: {head_sha!r}")


def pr_review_workspace_key(repo: str, number: int, head_sha: str) -> str:
    return f"{workspace_key(repo, number)}__{_validate_pr_head_sha(head_sha)}"


def _safe_directory_env(repo_dir: Path) -> dict[str, str]:
    """Return a Git config env overlay whitelisting ``repo_dir`` as safe."""
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(repo_dir),
    }


def _git_env_for_repo(repo_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(_safe_directory_env(repo_dir))
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def make_branch(*, issue_number: int, title: str, seed: str | None = None) -> str:
    return f"farm/{_short_hex(seed or f'{issue_number}-{title}')}/{_slug(title or f'issue-{issue_number}')}"


_BRANCH_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate_branch_slug(slug: object) -> str:
    """Return ``slug`` if it is a valid kebab-case branch slug, else raise.

    Rules: 1-50 chars, only ``[a-z0-9-]``, no leading/trailing hyphen, no
    double hyphen. Raises ``ValueError`` otherwise.
    """
    if not isinstance(slug, str) or not _BRANCH_SLUG_RE.fullmatch(slug) or len(slug) > 50:
        raise ValueError(
            f"invalid branch slug {slug!r}: expected kebab-case [a-z0-9-], 1-50 chars, no leading/trailing/double hyphen"
        )
    return slug


def rename_workspace_branch(
    workspace: Workspace,
    new_slug: str,
    *,
    pr_number: int | None = None,
    slot_uid: int | None = None,
) -> str:
    """Rename the workspace's local branch to ``farm/<hex>/<new_slug>``.

    The 8-hex disambiguator stays untouched; only the trailing slug after
    the second `/` changes. Runs ``git branch -m`` inside the worktree
    (which updates the shared refs in the pool) and mutates
    ``workspace.branch`` in place.

    Idempotent when the computed branch already matches ``workspace.branch``.
    Raises ``ValueError`` for syntactically invalid slugs or for a
    workspace whose branch isn't on the ``farm/<hex>/<slug>`` shape.
    Raises ``GitCommandError`` if the underlying ``git`` invocation fails
    (e.g. the target branch name is already taken).

    When ``pr_number`` is provided (non-None), the rename is a no-op: an
    open PR on origin still tracks ``workspace.branch``, and renaming it
    locally would orphan the PR by leaving its head on a branch that no
    longer receives pushes. The slug is still validated so callers see
    the same input errors as the rename path.
    """
    validate_branch_slug(new_slug)
    parts = workspace.branch.split("/", 2)
    if len(parts) != 3 or parts[0] != "farm" or not parts[1]:
        raise ValueError(f"refusing to rename non-farm branch {workspace.branch!r}")
    new_branch = f"farm/{parts[1]}/{new_slug}"
    if new_branch == workspace.branch:
        return new_branch
    if pr_number is not None:
        log.warning(
            "rename_workspace_branch skipped: PR #%d already tracks %r; refusing to rename to %r",
            pr_number,
            workspace.branch,
            new_branch,
        )
        return workspace.branch
    proc = _safe_run(
        ["git", "branch", "-m", workspace.branch, new_branch],
        cwd=workspace.repo_dir,
        timeout=_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
        env=_git_env_for_repo(workspace.repo_dir),
        **_slot_subprocess_kwargs(slot_uid),
    )
    if proc.returncode != 0:
        raise GitCommandError(
            ["git", "branch", "-m", workspace.branch, new_branch],
            proc.returncode,
            proc.stdout,
            proc.stderr,
        )
    _share_git_metadata_with_slots(workspace.repo_dir, slot_uid)
    workspace.branch = new_branch
    return new_branch


# ---------- GitTransport (transport abstraction over clone/fetch/push) ----------


class GitTransport(Protocol):
    """Pluggable remote-facing git operations.

    Two implementations ship in-tree:
    - `LocalGitTransport`: in-process git with PAT injected per invocation.
    - `robomp.proxy_client.ProxyGitTransport`: forwards over HMAC RPC.
    """

    def clone_pool(self, *, repo: str, clone_url: str, default_branch: str, target: Path) -> None:
        """Fresh clone into `target`. `target` must not exist (or be empty)."""
        ...

    def fetch_pool(self, *, repo: str, pool_dir: Path) -> None:
        """`git fetch --prune origin` against the shared pool clone."""
        ...

    def fetch_base_ref(self, *, repo: str, pool_dir: Path, ref: str) -> None:
        """Best-effort `git fetch origin <ref>` to ensure the base branch is local."""
        ...

    def fetch_pr_head(self, *, repo: str, pool_dir: Path, pr_number: int) -> None:
        """Fetch `refs/pull/<n>/head` into FETCH_HEAD for detached PR review checkouts."""
        ...

    def prepare_pr_worktree(
        self,
        *,
        repo: str,
        pool_dir: Path,
        repo_dir: Path,
        pr_number: int,
        expected_head_sha: str,
        base_ref: str,
        changed_paths: Iterable[str],
    ) -> PrWorktreeResult:
        """Create a sparse detached PR review worktree through the token-bearing transport."""
        ...

    def push_branch(
        self,
        *,
        repo: str,
        workspace_key: str,
        repo_dir: Path,
        branch: str,
        expected_head: str,
        slot_uid: int | None = None,
    ) -> PushResult:
        """Push `branch` to origin. MUST refuse if HEAD has drifted from `expected_head`."""
        ...


class LocalGitTransport:
    """Default GitTransport: run git in-process with ephemeral PAT injection.

    `token` MAY be `None` for tests against a local bare repo (no auth) or in
    deploys where the orchestrator does not hold a PAT (but then the proxy
    transport should be used instead).
    """

    __slots__ = ("_token",)

    def __init__(self, token: str | None) -> None:
        self._token = token

    def clone_pool(self, *, repo: str, clone_url: str, default_branch: str, target: Path) -> None:
        del repo  # unused; URL identifies the remote
        git_clone(target, clone_url=clone_url, default_branch=default_branch, token=self._token)

    def fetch_pool(self, *, repo: str, pool_dir: Path) -> None:
        del repo
        git_fetch_prune(pool_dir, token=self._token)

    def fetch_base_ref(self, *, repo: str, pool_dir: Path, ref: str) -> None:
        del repo
        git_fetch_ref(pool_dir, ref, token=self._token)

    def fetch_pr_head(self, *, repo: str, pool_dir: Path, pr_number: int) -> None:
        del repo
        git_fetch_pr_head(pool_dir, pr_number, token=self._token)

    def prepare_pr_worktree(
        self,
        *,
        repo: str,
        pool_dir: Path,
        repo_dir: Path,
        pr_number: int,
        expected_head_sha: str,
        base_ref: str,
        changed_paths: Iterable[str],
    ) -> PrWorktreeResult:
        del repo
        return git_prepare_pr_worktree(
            pool_dir,
            repo_dir,
            pr_number=pr_number,
            expected_head_sha=expected_head_sha,
            base_ref=base_ref,
            changed_paths=changed_paths,
            token=self._token,
        )

    def push_branch(
        self,
        *,
        repo: str,
        workspace_key: str,
        repo_dir: Path,
        branch: str,
        expected_head: str,
        slot_uid: int | None = None,
    ) -> PushResult:
        del repo, workspace_key
        return git_push(repo_dir, branch=branch, expected_head=expected_head, token=self._token, slot_uid=slot_uid)


# ---------- low-level helpers retained for callers expecting old shape ----------


def _safe_run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = _DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run without raising; caller decides on returncode. Credentials are redacted from any captured output."""
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        **kwargs,
    )
    if proc.stdout:
        proc.stdout = redact_credentials(proc.stdout)
    if proc.stderr:
        proc.stderr = redact_credentials(proc.stderr)
    return proc


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = _DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Legacy raising helper (still used by a sandbox test). Forwards to subprocess.run."""
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise GitCommandError(cmd, proc.returncode, proc.stdout, proc.stderr)
    return proc


_SHARED_OMP_GID = 2000


def _slot_permissions_active(slot_uid: int | None) -> bool:
    return slot_uid is not None and platform.system() == "Linux" and os.geteuid() == 0

_ROOT_CONTROL_DIRS = frozenset({".omp-session", "context", "artifacts", ".omp-tmp", ".omp-xdg"})


def _safe_workspace_child(root: Path, relative: str) -> Path:
    """Return ``root / relative`` only when it stays inside ``root``."""
    if "\0" in relative:
        raise ValueError("workspace-relative path contains NUL byte")
    raw = Path(relative)
    if raw.is_absolute():
        raise ValueError("workspace-relative path must not be absolute")
    candidate = root / raw
    resolved_root = root.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if resolved_candidate != resolved_root and resolved_root not in resolved_candidate.parents:
        raise ValueError("workspace-relative path escapes workspace root")
    return candidate


def _open_dir_no_follow(path: Path) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _ensure_root_control_dir(path: Path) -> None:
    """Create a root-owned control directory without following a planted symlink."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(st.st_mode):
            path.unlink()
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd = _open_dir_no_follow(path)
    try:
        if os.geteuid() == 0:
            os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o755)
    finally:
        os.close(fd)


def _ensure_real_dir(path: Path, *, mode: int = 0o755) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(st.st_mode):
            path.unlink()
    path.mkdir(mode=mode, parents=True, exist_ok=True)


def _ensure_slot_writable_dir(path: Path, slot_uid: int | None, *, mode: int = 0o700) -> None:
    _ensure_real_dir(path, mode=mode)
    fd = _open_dir_no_follow(path)
    try:
        if _slot_permissions_active(slot_uid):
            assert slot_uid is not None
            os.fchown(fd, slot_uid, slot_uid)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _slot_tmp_leaf(tmp_root: Path, slot_uid: int | None) -> Path:
    return tmp_root / f"slot-{slot_uid}" if _slot_permissions_active(slot_uid) else tmp_root



def _slot_pids(slot_uid: int, proc_root: Path = Path("/proc")) -> tuple[int, ...]:
    """Return non-zombie process ids owned by the slot UID.

    Debian's slim image does not include procps/pkill. Reading `/proc` keeps
    slot cleanup self-contained and avoids adding a runtime package only for
    this one operation.
    """
    try:
        entries = tuple(proc_root.iterdir())
    except OSError as exc:
        log.warning("failed to scan %s for slot user %s: %s", proc_root, slot_uid, exc)
        return ()

    pids: list[int] = []
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8")
        except OSError:
            # The process may have exited between `iterdir` and `read_text`.
            continue

        state = ""
        uids: tuple[int, ...] = ()
        for line in status.splitlines():
            if line.startswith("State:"):
                parts = line.split(maxsplit=1)
                state = parts[1] if len(parts) == 2 else ""
            elif line.startswith("Uid:"):
                try:
                    uids = tuple(int(part) for part in line.split()[1:5])
                except ValueError:
                    uids = ()

        if state.startswith("Z"):
            continue
        if slot_uid in uids:
            pids.append(int(entry.name))
    return tuple(pids)


def _reap_slot(slot_uid: int | None) -> None:
    """Kill any processes still running as a slot UID.

    Slot UIDs are reused. A previous task's straggler process must not survive
    long enough to observe or interfere with the next task assigned to that UID.
    """
    if not _slot_permissions_active(slot_uid):
        return
    assert slot_uid is not None
    for pid in _slot_pids(slot_uid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except OSError as exc:
            log.warning("failed to kill slot user %s process %s: %s", slot_uid, pid, exc)


def _prepare_slot_tmpdir(workspace: Workspace, slot_uid: int | None) -> Path:
    """Return a symlink-safe temp directory for slot-side subprocesses."""
    tmp_root = workspace.root / ".omp-tmp"
    _ensure_root_control_dir(tmp_root)
    tmpdir = _slot_tmp_leaf(tmp_root, slot_uid)
    if tmpdir != tmp_root:
        _ensure_slot_writable_dir(tmpdir, slot_uid)
    return tmpdir


def _slot_subprocess_kwargs(slot_uid: int | None) -> dict[str, Any]:
    """Return subprocess identity kwargs for commands that should run as a slot.

    `preexec_fn` is intentionally avoided: the worker runs tasks in threads,
    and `subprocess` warns that `preexec_fn` is unsafe in multithreaded
    parents. Python's native `user` / `group` / `extra_groups` parameters do
    the setuid/setgid work in the child safely.
    """
    if not _slot_permissions_active(slot_uid):
        return {}
    assert slot_uid is not None
    return {"user": slot_uid, "group": slot_uid, "extra_groups": [_SHARED_OMP_GID], "umask": 0o002}


def _prepare_slot_runtime_env(workspace: Workspace, slot_uid: int | None) -> dict[str, str]:
    """Compute symlink-safe TMPDIR + XDG env for slot-side subprocesses."""
    tmpdir = _prepare_slot_tmpdir(workspace, slot_uid)
    xdg_root = workspace.root / ".omp-xdg"
    _ensure_root_control_dir(xdg_root)
    xdg_cache = xdg_root / "cache"
    xdg_config = xdg_root / "config"
    xdg_data = xdg_root / "data"
    xdg_state = xdg_root / "state"
    for base in (xdg_cache, xdg_config, xdg_data, xdg_state):
        _ensure_slot_writable_dir(base, slot_uid)
    bun_cache = xdg_cache / "bun-install"
    _ensure_slot_writable_dir(bun_cache, slot_uid)
    _ensure_slot_writable_dir(xdg_cache / "omp", slot_uid)
    _ensure_slot_writable_dir(xdg_data / "omp", slot_uid)
    _ensure_slot_writable_dir(xdg_state / "omp", slot_uid)

    return {
        "TMPDIR": str(tmpdir),
        "TMP": str(tmpdir),
        "TEMP": str(tmpdir),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_STATE_HOME": str(xdg_state),
        "XDG_CACHE_HOME": str(xdg_cache),
        "BUN_INSTALL_CACHE_DIR": str(bun_cache),
    }


def _provision_runtime_dirs(ws_root: Path) -> None:
    """Create symlink-safe root control dirs and runtime leaves."""
    for relative in (".omp-session", "context", "artifacts", ".omp-tmp", ".omp-xdg"):
        _ensure_root_control_dir(ws_root / relative)
    xdg_root = ws_root / ".omp-xdg"
    for sub in ("cache", "config", "data", "state"):
        _ensure_real_dir(xdg_root / sub, mode=0o700)
    _ensure_real_dir(xdg_root / "cache" / "omp", mode=0o700)
    _ensure_real_dir(xdg_root / "cache" / "bun-install", mode=0o700)
    _ensure_real_dir(xdg_root / "data" / "omp", mode=0o700)
    _ensure_real_dir(xdg_root / "state" / "omp", mode=0o700)
    _ensure_real_dir(ws_root / "artifacts" / "agent", mode=0o755)


def _grant_group_bits(path: Path, *, gid: int, bits: int) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        return
    os.chown(path, -1, gid)
    path.chmod(stat.S_IMODE(st.st_mode) | bits)


def _grant_tree(path: Path, *, gid: int, files_group_writable: bool) -> None:
    if not path.exists():
        return
    if path.is_file():
        bits = stat.S_IRGRP | (stat.S_IWGRP if files_group_writable else 0)
        _grant_group_bits(path, gid=gid, bits=bits)
        return
    for root, dirs, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        _grant_group_bits(root_path, gid=gid, bits=stat.S_IRWXG | stat.S_ISGID)
        for dirname in dirs:
            _grant_group_bits(root_path / dirname, gid=gid, bits=stat.S_IRWXG | stat.S_ISGID)
        file_bits = stat.S_IRGRP | (stat.S_IWGRP if files_group_writable else 0)
        for filename in files:
            _grant_group_bits(root_path / filename, gid=gid, bits=file_bits)


def _resolve_worktree_git_dirs(repo_dir: Path) -> tuple[Path, Path] | None:
    marker = repo_dir / ".git"
    if marker.is_dir():
        return marker, marker
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not text.startswith(prefix):
        return None
    raw_git_dir = text[len(prefix) :].strip()
    git_dir = Path(raw_git_dir)
    if not git_dir.is_absolute():
        git_dir = (repo_dir / git_dir).resolve()
    try:
        raw_common_dir = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return git_dir, git_dir
    common_dir = Path(raw_common_dir)
    if not common_dir.is_absolute():
        common_dir = (git_dir / common_dir).resolve()
    return git_dir, common_dir


def _share_git_metadata_with_slots(repo_dir: Path, slot_uid: int | None) -> None:
    """Pool Git metadata stays root-owned/read-only; remote writes use GitTransport."""
    del repo_dir, slot_uid
    return



def _chmod_chown_no_symlink(path: Path, *, uid: int, gid: int, mode: int) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        return
    os.chown(path, uid, gid, follow_symlinks=False)
    os.chmod(path, mode, follow_symlinks=False)


def _chown_workspace_tree(ws_root: Path, *, uid: int, gid: int) -> None:
    for current_root, dirs, files in os.walk(ws_root, topdown=True, followlinks=False):
        current = Path(current_root)
        if current == ws_root:
            dirs[:] = [dirname for dirname in dirs if dirname not in _ROOT_CONTROL_DIRS]
        _chmod_chown_no_symlink(current, uid=uid, gid=gid, mode=0o770)
        for dirname in dirs:
            _chmod_chown_no_symlink(current / dirname, uid=uid, gid=gid, mode=0o770)
        for filename in files:
            _chmod_chown_no_symlink(current / filename, uid=uid, gid=gid, mode=0o660)



def _chown_slot_leaf_tree(path: Path, slot_uid: int) -> None:
    _ensure_slot_writable_dir(path, slot_uid, mode=0o700)
    for current_root, dirs, files in os.walk(path, topdown=True, followlinks=False):
        current = Path(current_root)
        _chmod_chown_no_symlink(current, uid=slot_uid, gid=slot_uid, mode=0o700)
        for dirname in dirs:
            _chmod_chown_no_symlink(current / dirname, uid=slot_uid, gid=slot_uid, mode=0o700)
        for filename in files:
            _chmod_chown_no_symlink(current / filename, uid=slot_uid, gid=slot_uid, mode=0o600)

def _chown_slot_leaves(ws_root: Path, slot_uid: int | None) -> None:
    if slot_uid is None:
        return
    leaves = [
        _slot_tmp_leaf(ws_root / ".omp-tmp", slot_uid),
        ws_root / ".omp-xdg" / "cache",
        ws_root / ".omp-xdg" / "config",
        ws_root / ".omp-xdg" / "data",
        ws_root / ".omp-xdg" / "state",
        ws_root / ".omp-xdg" / "cache" / "omp",
        ws_root / ".omp-xdg" / "cache" / "bun-install",
        ws_root / ".omp-xdg" / "data" / "omp",
        ws_root / ".omp-xdg" / "state" / "omp",
        ws_root / "artifacts" / "agent",
    ]
    for path in leaves:
        _chown_slot_leaf_tree(path, slot_uid)

def _chown_workspace(ws_root: Path, slot_uid: int | None) -> None:
    """Hand repo checkout to slot while keeping control dirs root-owned."""
    if platform.system() != "Linux":
        return
    if os.geteuid() != 0:
        return
    uid = slot_uid if slot_uid is not None else os.geteuid()
    gid = slot_uid if slot_uid is not None else os.getegid()
    _chown_workspace_tree(ws_root, uid=uid, gid=gid)
    for name in _ROOT_CONTROL_DIRS:
        control = ws_root / name
        if control.exists() or control.is_symlink():
            _ensure_root_control_dir(control)
    _chown_slot_leaves(ws_root, slot_uid)


# ---------- SandboxManager ----------


class SandboxManager:
    """Manages a shared clone pool and per-issue worktrees.

    Remote-facing git operations are delegated to a `GitTransport`; the rest
    (worktree add/remove, identity config, directory layout) is purely local.
    """

    def __init__(
        self,
        root: Path,
        *,
        transport: GitTransport | None = None,
        natives_cache: NativesCache | None = None,
    ) -> None:
        self.root = root
        self.pool = root / "_pool"
        self.transport: GitTransport = transport or LocalGitTransport(token=None)
        self.natives_cache = natives_cache
        root.mkdir(parents=True, exist_ok=True)
        self.pool.mkdir(parents=True, exist_ok=True)

    # ---- pool ----
    def pool_path(self, repo: str) -> Path:
        return self.pool / repo.replace("/", "__")

    def ensure_clone(self, *, repo: str, clone_url: str, default_branch: str) -> Path:
        """Idempotent shared clone for `repo`.

        `clone_url` MUST be a plain `https://github.com/<owner>/<repo>.git`
        (no embedded credentials). Auth is supplied per-call by the transport.
        """
        target = self.pool_path(repo)
        if (target / ".git").exists() or (target / "HEAD").exists():
            # Idempotent refresh. An older deploy may have baked a
            # credentialed `https://user:pass@github.com/...` into
            # `.git/config`; rewrite to the credential-free URL we now own
            # before fetching so the PAT never persists on disk.
            self._reset_origin_url(target, clone_url)
            self.transport.fetch_pool(repo=repo, pool_dir=target)
            return target
        target.mkdir(parents=True, exist_ok=True)
        self.transport.clone_pool(
            repo=repo,
            clone_url=clone_url,
            default_branch=default_branch,
            target=target,
        )
        return target

    @staticmethod
    def _reset_origin_url(repo_dir: Path, clone_url: str) -> None:
        """`git remote set-url origin <clone_url>` if origin exists and differs.

        Best-effort: silent no-op on failure (probe `get-url` first so we don't
        spam logs on first-time clones where origin isn't configured yet).
        """
        probe = _safe_run(["git", "remote", "get-url", "origin"], cwd=repo_dir, timeout=_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS)
        if probe.returncode != 0:
            return
        if probe.stdout.strip() == clone_url:
            return
        _safe_run(["git", "remote", "set-url", "origin", clone_url], cwd=repo_dir, timeout=_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS)

    # ---- per-issue workspace ----
    def workspace_root(self, repo: str, number: int, head_sha: str | None = None) -> Path:
        if head_sha is None:
            key = workspace_key(repo, number)
        else:
            key = pr_review_workspace_key(repo, number, head_sha)
        return self.root / key

    def _remove_workspace_root(self, repo: str, ws_root: Path) -> None:
        repo_dir = ws_root / "repo"
        if repo_dir.exists():
            pool = self.pool_path(repo)
            _safe_run(["git", "worktree", "remove", "--force", str(repo_dir)], cwd=pool, timeout=_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS)
            if repo_dir.exists():
                shutil.rmtree(repo_dir, ignore_errors=True)
        if ws_root.exists():
            shutil.rmtree(ws_root, ignore_errors=True)

    @staticmethod
    def _read_pr_review_manifest(session_dir: Path) -> dict[str, Any] | None:
        path = session_dir / "pr-review-workspace.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _write_pr_review_manifest(
        session_dir: Path,
        *,
        repo: str,
        number: int,
        head_sha: str,
        base_ref: str,
        changed_paths: tuple[str, ...],
        hydrated_paths: tuple[str, ...],
    ) -> None:
        (session_dir / "pr-review-workspace.json").write_text(
            json.dumps(
                {
                    "repo": repo,
                    "pr_number": number,
                    "head_sha": head_sha,
                    "base_ref": base_ref,
                    "changed_paths": list(changed_paths),
                    "hydrated_paths": list(hydrated_paths),
                    "prepared_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
                sort_keys=True,
            )
        )

    def ensure_workspace(
        self,
        *,
        repo: str,
        number: int,
        title: str,
        clone_url: str,
        default_branch: str,
        existing_branch: str | None = None,
        pr_head: int | None = None,
        pr_head_sha: str | None = None,
        pr_base_ref: str | None = None,
        pr_changed_paths: Iterable[str] | None = None,
        author_name: str,
        author_email: str,
        slot_uid: int | None = None,
    ) -> Workspace:
        """Create or resume a per-issue worktree."""
        if pr_head is not None and existing_branch is not None:
            raise ValueError("ensure_workspace accepts either pr_head or existing_branch, not both")
        if pr_head is None and pr_head_sha is not None:
            raise ValueError("pr_head_sha is only valid for PR review workspaces")
        requested_head_sha = _validate_pr_head_sha(pr_head_sha) if pr_head_sha is not None else None
        if pr_head is not None and requested_head_sha is None:
            raise ValueError("PR review workspace requires pr_head_sha")
        requested_changed_paths = tuple(pr_changed_paths or ())
        pool = self.ensure_clone(repo=repo, clone_url=clone_url, default_branch=default_branch)
        ws_root = self.workspace_root(repo, number, head_sha=requested_head_sha)
        repo_dir = ws_root / "repo"
        session_dir = _safe_workspace_child(ws_root, ".omp-session")
        context_dir = _safe_workspace_child(ws_root, "context")
        artifacts_root = _safe_workspace_child(ws_root, "artifacts")
        artifacts_dir = artifacts_root / "agent"

        def prepare_control_dirs() -> None:
            ws_root.mkdir(parents=True, exist_ok=True)
            for path in (session_dir, context_dir, artifacts_root, ws_root / ".omp-tmp", ws_root / ".omp-xdg"):
                _ensure_root_control_dir(path)
            _ensure_real_dir(context_dir / "repro", mode=0o755)
            _ensure_real_dir(artifacts_dir, mode=0o755)

        prepare_control_dirs()

        branch = (
            f"review/pr-{pr_head}"
            if pr_head is not None
            else existing_branch
            or make_branch(
                issue_number=number,
                title=title,
                seed=f"{repo}#{number}",
            )
        )

        repo_exists = (repo_dir / ".git").exists()
        workspace_prepared = False
        slot_git_kwargs = _slot_subprocess_kwargs(slot_uid)
        slot_git_env: dict[str, str] | None = None
        if repo_exists:
            # Existing workspaces are already slot-owned from the previous run.
            # Refresh pool-side group bits, then hand the tree to the current
            # slot before running any git command inside the worktree; root's
            # uid-0 bypass does not bypass git's safe.directory ownership check.
            _share_git_metadata_with_slots(repo_dir, slot_uid)
            _provision_runtime_dirs(ws_root)
            _chown_workspace(ws_root, slot_uid)
            workspace_prepared = True
            if requested_head_sha is not None:
                manifest = self._read_pr_review_manifest(session_dir)
                slot_git_env = _git_env_for_repo(repo_dir)
                local_head = _safe_run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo_dir,
                    timeout=_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
                    env=slot_git_env,
                    **slot_git_kwargs,
                )
                expected_patterns: tuple[str, ...] = ()
                if requested_changed_paths:
                    try:
                        expected_patterns = pr_sparse_checkout_patterns(requested_changed_paths)
                    except ValueError:
                        expected_patterns = ()
                manifest_matches = (
                    manifest is not None
                    and manifest.get("head_sha") == requested_head_sha
                    and manifest.get("base_ref") == pr_base_ref
                    and tuple(manifest.get("changed_paths") or ()) == requested_changed_paths
                    # Rebuild when the sparse-hydration spec changed (stale worktrees
                    # prepared before a checkout-strategy change carry old patterns).
                    and tuple(manifest.get("hydrated_paths") or ()) == expected_patterns
                )
                head_matches = local_head.returncode == 0 and local_head.stdout.strip().lower() == requested_head_sha
                if not manifest_matches or not head_matches:
                    self._remove_workspace_root(repo, ws_root)
                    prepare_control_dirs()
                    repo_exists = False
                    workspace_prepared = False
                    slot_git_env = None
        if not repo_exists:
            if pr_head is not None:
                if pr_base_ref is None or pr_changed_paths is None:
                    raise ValueError("PR review workspace requires pr_base_ref and pr_changed_paths")
                assert requested_head_sha is not None
                result = self.transport.prepare_pr_worktree(
                    repo=repo,
                    pool_dir=pool,
                    repo_dir=repo_dir,
                    pr_number=pr_head,
                    expected_head_sha=requested_head_sha,
                    base_ref=pr_base_ref,
                    changed_paths=requested_changed_paths,
                )
                if result.head.lower() != requested_head_sha:
                    self._remove_workspace_root(repo, ws_root)
                    raise ValueError(f"prepared PR worktree head mismatch: expected {requested_head_sha}, got {result.head}")
                self._write_pr_review_manifest(
                    session_dir,
                    repo=repo,
                    number=pr_head,
                    head_sha=requested_head_sha,
                    base_ref=pr_base_ref,
                    changed_paths=requested_changed_paths,
                    hydrated_paths=result.hydrated_paths,
                )
            else:
                # Make sure the requested start point exists locally (best-effort).
                # For follow-ups on an existing PR, `existing_branch` is the remote
                # head branch we need to amend; starting from default would silently
                # lose the PR's current commits if the local pool branch is absent.
                self.transport.fetch_base_ref(repo=repo, pool_dir=pool, ref=existing_branch or default_branch)
                check = _safe_run(
                    ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
                    cwd=pool,
                    timeout=_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
                )
                if check.returncode == 0:
                    _run(["git", "worktree", "add", str(repo_dir), branch], cwd=pool, timeout=_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS)
                else:
                    start_point = f"origin/{default_branch}"
                    if existing_branch:
                        remote = _safe_run(
                            ["git", "rev-parse", "--verify", f"refs/remotes/origin/{existing_branch}"],
                            cwd=pool,
                            timeout=_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
                        )
                        if remote.returncode == 0:
                            start_point = f"origin/{existing_branch}"
                    _run(
                        [
                            "git",
                            "worktree",
                            "add",
                            "-b",
                            branch,
                            str(repo_dir),
                            start_point,
                        ],
                        cwd=pool,
                        timeout=_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
                    )
        elif requested_head_sha is None:
            slot_git_env = _git_env_for_repo(repo_dir)
            current = _safe_run(
                ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
                cwd=repo_dir,
                timeout=_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
                env=slot_git_env,
                **slot_git_kwargs,
            )
            if current.returncode == 0 and current.stdout.strip():
                branch = current.stdout.strip()
                if existing_branch is not None and existing_branch != branch:
                    log.warning(
                        "workspace branch mapping %r differs from checked-out branch %r; using checkout",
                        existing_branch,
                        branch,
                    )
        if not workspace_prepared:
            _share_git_metadata_with_slots(repo_dir, slot_uid)
            _provision_runtime_dirs(ws_root)
            _chown_workspace(ws_root, slot_uid)
        if slot_git_env is None:
            slot_git_env = _git_env_for_repo(repo_dir)
        # Identity is set on the worktree's shared config; idempotent. Run as
        # the slot after the chown so git never trips over safe.directory.
        for command in (["git", "config", "user.email", author_email], ["git", "config", "user.name", author_name]):
            proc = _safe_run(command, cwd=repo_dir, timeout=_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS, env=slot_git_env, **slot_git_kwargs)
            if proc.returncode != 0:
                raise GitCommandError(command, proc.returncode, proc.stdout, proc.stderr)
        _share_git_metadata_with_slots(repo_dir, slot_uid)
        workspace = Workspace(
            root=ws_root,
            repo_dir=repo_dir,
            session_dir=session_dir,
            context_dir=context_dir,
            artifacts_dir=artifacts_dir,
            branch=branch,
            repo_full_name=repo,
            issue_number=number,
            review_head_sha=requested_head_sha,
        )
        # Best-effort: hardlink pre-built natives in if we've cached this
        # source state before. Runs AFTER the slot chown so the cache inode
        # keeps its `root:omp` ownership (the slot reads through group `omp`);
        # write-temp + rename in the napi build replaces with a new inode if
        # the agent rebuilds, so the cached file is never mutated.
        self._populate_natives_cache(workspace, slot_uid=slot_uid)
        return workspace

    def _populate_natives_cache(self, workspace: Workspace, *, slot_uid: int | None = None) -> None:
        """Try to hardlink cached pi-natives artifacts into the worktree.

        Best-effort: any failure (no cache configured, non-git worktree,
        cache miss, link error) is logged at debug and swallowed. The agent
        falls back to a fresh napi build, exactly as it would without the
        cache.

        Post-populate, the populated `packages/natives/native/` directory
        and the COPIED companion files are chowned to the slot so the slot
        can rebuild via temp + rename in that directory. The hardlinked
        `.node` files are LEFT at `root:omp` ownership — chowning them
        would chown the cache file too (shared inode), breaking the
        cross-slot sharing model. The slot reads them via group `omp`.
        """
        cache = self.natives_cache
        if cache is None:
            return
        native_dir = workspace.repo_dir / "packages" / "natives" / "native"
        # NOTE: we deliberately do NOT require `native_dir.exists()` here. On
        # a cache miss `populate_workspace` returns None without creating any
        # directory; on a hit it mkdirs and copies in. That's the right
        # behavior — a hit by definition implies this repo's source state
        # produces natives, so creating the dir is correct.
        try:
            key = natives_compute_key(workspace.repo_dir)
        except (subprocess.CalledProcessError, RuntimeError, OSError) as exc:
            log.debug(
                "natives_cache key compute failed",
                extra={"workspace": workspace.workspace_key, "err": redact_credentials(str(exc))},
            )
            return
        try:
            hit = cache.populate_workspace(workspace.repo_full_name, key, native_dir)
        except OSError as exc:
            log.warning(
                "natives_cache populate failed",
                extra={"workspace": workspace.workspace_key, "key": key, "err": str(exc)},
            )
            return
        if hit is not None and _slot_permissions_active(slot_uid):
            assert slot_uid is not None
            self._chown_natives_for_slot(native_dir, hit, slot_uid=slot_uid)
        log.info(
            "natives_cache",
            extra={
                "action": "hit" if hit is not None else "miss",
                "workspace": workspace.workspace_key,
                "repo": workspace.repo_full_name,
                "key": key,
                "files": [str(p.name) for p in hit.files] if hit is not None else [],
            },
        )

    @staticmethod
    def _chown_natives_for_slot(native_dir: Path, hit: CacheHit, *, slot_uid: int) -> None:
        """Hand the populated native dir to the slot WITHOUT touching the
        hardlinked `.node` inodes (those are shared with the cache).

        Files whose names match a cached `.node` are skipped — they are
        hardlinks back into the root:omp cache and the slot reads them via
        group `omp`. Everything else (the directory itself, copied
        companions) is chowned to the slot so the slot can rebuild via
        temp + rename.
        """
        try:
            os.chown(native_dir, slot_uid, slot_uid)
        except OSError as exc:
            log.warning("natives_cache chown dir failed", extra={"err": str(exc)})
            return
        node_basenames = {p.name for p in hit.files if p.name.endswith(".node")}
        for child in native_dir.iterdir():
            if child.name in node_basenames:
                continue  # hardlink to cache — must not chown
            try:
                os.chown(child, slot_uid, slot_uid, follow_symlinks=False)
            except OSError as exc:
                log.warning(
                    "natives_cache chown companion failed",
                    extra={"file": str(child), "err": str(exc)},
                )

    def remove_workspace(self, *, repo: str, number: int, head_sha: str | None = None) -> None:
        if head_sha is not None:
            self._remove_workspace_root(repo, self.workspace_root(repo, number, head_sha=head_sha))
            return
        legacy_root = self.workspace_root(repo, number)
        self._remove_workspace_root(repo, legacy_root)
        prefix = workspace_key(repo, number) + "__"
        for child in tuple(self.root.iterdir()):
            if child.is_dir() and child.name.startswith(prefix):
                self._remove_workspace_root(repo, child)

    # ---- workspace garbage collection ----
    @staticmethod
    def _parse_workspace_entry(child: Path) -> WorkspaceGcEntry | None:
        """Resolve a direct child of the workspace root into a GC entry.

        Prefers the PR-review manifest; falls back to the directory name.
        Returns ``None`` for ``_pool``, non-directories, and unparseable
        names so GC never deletes directories it cannot identify.
        """
        if child.name == "_pool" or not child.is_dir():
            return None
        repo: str | None = None
        number: int | None = None
        head_sha: str | None = None
        manifest = SandboxManager._read_pr_review_manifest(child / ".omp-session")
        if manifest is not None:
            m_repo = manifest.get("repo")
            m_number = manifest.get("pr_number")
            m_head = manifest.get("head_sha")
            if (
                isinstance(m_repo, str)
                and isinstance(m_number, int)
                and not isinstance(m_number, bool)
                and isinstance(m_head, str)
                and _HEX40_RE.fullmatch(m_head)
            ):
                repo, number, head_sha = m_repo, m_number, m_head.lower()
        if repo is None or number is None:
            parsed = _parse_workspace_dir_name(child.name)
            if parsed is None:
                return None
            repo, number, head_sha = parsed
        try:
            mtime = child.stat().st_mtime
        except OSError:
            return None
        return WorkspaceGcEntry(
            root=child,
            repo=repo,
            number=number,
            head_sha=head_sha,
            mtime=mtime,
            size_bytes=_dir_size_bytes(child),
        )

    def iter_workspace_entries(self) -> tuple[WorkspaceGcEntry, ...]:
        """Return parsed workspace entries sorted oldest-first by mtime."""
        if not self.root.exists():
            return ()
        entries: list[WorkspaceGcEntry] = []
        for child in self.root.iterdir():
            entry = self._parse_workspace_entry(child)
            if entry is not None:
                entries.append(entry)
        entries.sort(key=lambda e: (e.mtime, e.root.name))
        return tuple(entries)

    def workspace_storage_stats(self) -> WorkspaceStorageStats:
        """Summed workspace byte usage plus disk free/total for metrics."""
        entries = self.iter_workspace_entries()
        total_size = sum(e.size_bytes for e in entries)
        usage_path = self.root if self.root.exists() else self.root.parent
        try:
            usage = shutil.disk_usage(usage_path)
            free_bytes, total_bytes = usage.free, usage.total
        except OSError:
            free_bytes = total_bytes = 0
        return WorkspaceStorageStats(
            entries=len(entries),
            bytes=total_size,
            free_bytes=free_bytes,
            total_bytes=total_bytes,
        )

    def remove_superseded_pr_review_workspaces(self, *, repo: str, number: int, keep_head_sha: str) -> int:
        """Remove same-PR review workspaces whose head SHA is not ``keep_head_sha``."""
        keep = _validate_pr_head_sha(keep_head_sha)
        if not self.root.exists():
            return 0
        prefix = workspace_key(repo, number) + "__"
        removed = 0
        for child in tuple(self.root.iterdir()):
            if not child.is_dir() or not child.name.startswith(prefix):
                continue
            suffix = child.name[len(prefix):]
            if not _HEX40_RE.fullmatch(suffix) or suffix.lower() == keep:
                continue
            self._remove_workspace_root(repo, child)
            removed += 1
            log.info(
                "pr_review workspace superseded head removed",
                extra={"repo": repo, "pr": number, "head": suffix.lower(), "path": str(child)},
            )
        return removed

    def gc_workspaces(
        self,
        *,
        max_age_seconds: float,
        max_bytes: int,
        min_free_bytes: int,
        protected_issue_keys: frozenset[str] = frozenset(),
        now: float | None = None,
    ) -> WorkspaceGcResult:
        """Evict workspaces by TTL, then by size/free-space pressure, oldest-first.

        Protected issue keys are never evicted. ``_pool`` is never touched.
        Returns counts/bytes estimated before deletion; deletion is best-effort.
        """
        entries = self.iter_workspace_entries()
        scanned = len(entries)
        usage_path = self.root if self.root.exists() else self.root.parent
        try:
            usage = shutil.disk_usage(usage_path)
            initial_free, disk_total = usage.free, usage.total
        except OSError:
            initial_free = disk_total = 0
        total_workspace_bytes = sum(e.size_bytes for e in entries)
        now_ts = now if now is not None else time.time()
        candidates = [e for e in entries if e.issue_key not in protected_issue_keys]
        evict_roots: set[Path] = set()
        freed = 0

        if max_age_seconds > 0:
            for entry in candidates:
                if now_ts - entry.mtime >= max_age_seconds:
                    evict_roots.add(entry.root)
                    freed += entry.size_bytes

        def needs_more() -> bool:
            if max_bytes > 0 and (total_workspace_bytes - freed) > max_bytes:
                return True
            if min_free_bytes > 0 and (initial_free + freed) < min_free_bytes:
                return True
            return False

        for entry in candidates:
            if entry.root in evict_roots:
                continue
            if not needs_more():
                break
            evict_roots.add(entry.root)
            freed += entry.size_bytes

        evicted = 0
        freed_bytes = 0
        for entry in candidates:
            if entry.root not in evict_roots:
                continue
            try:
                self._remove_workspace_root(entry.repo, entry.root)
            except OSError as exc:
                log.warning(
                    "workspace gc remove failed",
                    extra={"path": str(entry.root), "err": str(exc)},
                )
                continue
            evicted += 1
            freed_bytes += entry.size_bytes

        return WorkspaceGcResult(
            evicted=evicted,
            freed_bytes=freed_bytes,
            total_bytes=disk_total,
            free_bytes=initial_free + freed_bytes,
            remaining_bytes=total_workspace_bytes - freed_bytes,
            scanned=scanned,
        )

    def gc_clone_pools(self) -> int:
        """Run ``git worktree prune`` + ``git gc --auto`` on each clone pool dir.

        Returns the count of pool dirs where either command succeeded. Best-effort:
        non-zero exits are logged, never raised.
        """
        if not self.pool.exists():
            return 0
        swept = 0
        for child in tuple(self.pool.iterdir()):
            if not child.is_dir():
                continue
            prune = _safe_run(
                ["git", "worktree", "prune"], cwd=child, timeout=_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS
            )
            collect = _safe_run(
                ["git", "gc", "--auto"], cwd=child, timeout=_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS
            )
            if prune.returncode == 0 or collect.returncode == 0:
                swept += 1
            if prune.returncode != 0:
                log.warning("clone pool worktree prune failed", extra={"pool": str(child), "rc": prune.returncode})
            if collect.returncode != 0:
                log.warning("clone pool gc failed", extra={"pool": str(child), "rc": collect.returncode})
        return swept


__all__ = [
    "GitCommandError",
    "GitTransport",
    "LocalGitTransport",
    "SandboxManager",
    "Workspace",
    "WorkspaceGcEntry",
    "WorkspaceGcResult",
    "WorkspaceStorageStats",
    "make_branch",
    "rename_workspace_branch",
    "validate_branch_slug",
    "redact_credentials",
    "workspace_key",
    "pr_review_workspace_key",
]
