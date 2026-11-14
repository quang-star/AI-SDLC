"""Safe subprocess Git operations, immutable SHA checkouts, diff parsing, and remote push."""

from __future__ import annotations

import hashlib
import os
import posixpath
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_sdlc.repo_policy import IGNORED_UPLOAD_DIRS
from ai_sdlc_saas.errors import BaselineStaleError, ValidationError
from ai_sdlc_saas.security.ssrf import validate_external_http_url


class GitOperationError(RuntimeError):
    """Raised when a subprocess git operation fails."""


@dataclass(frozen=True)
class NameStatusEntry:
    status: str
    path: str
    old_path: str | None = None


@dataclass(frozen=True)
class RawDiffEntry:
    old_mode: str
    new_mode: str
    status: str
    path: str


@dataclass(frozen=True)
class DiffBundle:
    base_commit: str
    patch_text: str
    patch_sha256: str
    name_status: tuple[NameStatusEntry, ...]
    raw_entries: tuple[RawDiffEntry, ...]
    untracked_files: tuple[str, ...]
    symlink_paths: tuple[str, ...]
    submodule_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]


def run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_sec: int = 120,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    merged_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    if env:
        merged_env.update(env)
    cmd = ["git", "-c", "core.autocrlf=false", "-c", "core.quotePath=false", *args]
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=merged_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
        check=False,
    )
    if proc.returncode != 0:
        raise GitOperationError(
            f"git {' '.join(args[:4])} failed (rc={proc.returncode}): {(proc.stderr or proc.stdout).strip()[:400]}"
        )
    return proc


def validate_repo_source(source: str, *, allow_local_repos: bool = False) -> tuple[str, bool]:
    """Validate whether `source` is an authorized remote HTTPS URL or local git directory."""
    cleaned = source.strip()
    if not cleaned:
        raise ValidationError("Repository URL or path cannot be empty.")
    if cleaned.startswith(("http://", "https://")):
        validated = validate_external_http_url(cleaned, allow_local=allow_local_repos)
        return validated, False
    if cleaned.startswith(("git@", "ssh://", "file://", "git://")):
        if not allow_local_repos and cleaned.startswith("file://"):
            raise ValidationError("Local file:// repositories are disabled in this environment.")
        if cleaned.startswith(("git://",)):
            raise ValidationError("Unauthenticated git:// protocol is not allowed; use HTTPS or GitHub App.")
        if cleaned.startswith("file://"):
            return cleaned, True
        return cleaned, False
    # Local filesystem path
    if not allow_local_repos:
        raise ValidationError("Local filesystem repositories are disabled unless explicitly enabled.")
    path = Path(cleaned)
    if not path.exists():
        raise ValidationError(f"Local repository path does not exist: {cleaned}")
    return str(path.resolve()), True


def init_bare_repo_from_dir(source_dir: Path, bare_repo_dir: Path, *, default_branch: str = "main") -> str:
    """Create a bare git remote initialized with a clean snapshot commit of `source_dir`."""
    src = Path(source_dir).resolve()
    bare = Path(bare_repo_dir).resolve()
    if bare.exists():
        shutil.rmtree(bare)
    bare.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="sdlc_init_") as tmp:
        stage = Path(tmp) / "work"
        shutil.copytree(
            src,
            stage,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules", ".venv", ".pytest_cache"),
        )
        run_git(["init", "-b", default_branch], cwd=stage)
        run_git(["config", "user.email", "sdlc-bot@ai-sdlc.local"], cwd=stage)
        run_git(["config", "user.name", "AI SDLC Bot"], cwd=stage)
        run_git(["add", "-A"], cwd=stage)
        run_git(["commit", "-m", "Initial baseline commit"], cwd=stage)
        sha = run_git(["rev-parse", "HEAD"], cwd=stage).stdout.strip()
        run_git(["clone", "--bare", str(stage), str(bare)])
        return sha


def clone_at_commit(
    source_url_or_path: str,
    dest_dir: Path,
    *,
    branch_or_sha: str | None = None,
    allow_local_repos: bool = False,
) -> tuple[str, str]:
    """Clone `source_url_or_path` into `dest_dir` cleanly and verify HEAD SHA."""
    validated, _is_local = validate_repo_source(source_url_or_path, allow_local_repos=allow_local_repos)
    dest = Path(dest_dir).resolve()
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    run_git(["clone", validated, str(dest)])
    if branch_or_sha:
        run_git(["checkout", branch_or_sha], cwd=dest)
    commit_sha = run_git(["rev-parse", "HEAD"], cwd=dest).stdout.strip()
    tree_sha = run_git(["rev-parse", "HEAD^{tree}"], cwd=dest).stdout.strip()
    return commit_sha, tree_sha


def _is_ignored_untracked(rel_posix: str) -> bool:
    parts = [p for p in rel_posix.split("/") if p]
    return any(p in IGNORED_UPLOAD_DIRS for p in parts)


def parse_name_status_z(raw_bytes: bytes) -> list[NameStatusEntry]:
    tokens = [t.decode("utf-8", errors="replace") for t in raw_bytes.split(b"\x00") if t]
    entries: list[NameStatusEntry] = []
    idx = 0
    while idx < len(tokens):
        code = tokens[idx]
        idx += 1
        if code.startswith(("R", "C")) and idx + 1 < len(tokens):
            old_p = tokens[idx]
            new_p = tokens[idx + 1]
            idx += 2
            entries.append(NameStatusEntry(status=code[0], path=new_p, old_path=old_p))
        elif idx < len(tokens):
            p = tokens[idx]
            idx += 1
            entries.append(NameStatusEntry(status=code[0], path=p))
    return entries


def parse_raw_diff_z(raw_bytes: bytes) -> list[RawDiffEntry]:
    tokens = [t.decode("utf-8", errors="replace") for t in raw_bytes.split(b"\x00") if t]
    entries: list[RawDiffEntry] = []
    idx = 0
    while idx < len(tokens):
        meta = tokens[idx]
        idx += 1
        if not meta.startswith(":") or idx >= len(tokens):
            continue
        path = tokens[idx]
        idx += 1
        fields = meta[1:].split()
        if len(fields) >= 5:
            old_mode, new_mode, _old_sha, _new_sha, status = fields[:5]
            entries.append(
                RawDiffEntry(old_mode=old_mode, new_mode=new_mode, status=status[0], path=path)
            )
    return entries


def collect_git_diff_bundle(worktree_dir: Path, base_commit: str) -> DiffBundle:
    """Capture NUL-safe name-status, raw mode entries, untracked files, and unified patch."""
    repo = Path(worktree_dir).resolve()
    untracked_proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=str(repo),
        capture_output=True,
        check=False,
    )
    untracked: list[str] = []
    for raw_name in untracked_proc.stdout.split(b"\x00"):
        if not raw_name:
            continue
        rel = raw_name.decode("utf-8", errors="replace").replace("\\", "/")
        if _is_ignored_untracked(rel):
            continue
        untracked.append(rel)

    # Use a temporary index file so working tree index state is never dirtied permanently.
    with tempfile.TemporaryDirectory(prefix="git_idx_") as tmp:
        temp_idx = Path(tmp) / "index"
        orig_idx = repo / ".git" / "index"
        if orig_idx.exists():
            shutil.copy2(orig_idx, temp_idx)
        env = {"GIT_INDEX_FILE": str(temp_idx)}

        # Only add tracked modifications + filtered untracked files (excluding ignored build/cache dirs).
        run_git(["add", "-u"], cwd=repo, env=env)
        for rel in untracked:
            run_git(["add", "--", rel], cwd=repo, env=env)

        ns_proc = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", "--cached", "--name-status", "-M", "-z", base_commit],
            cwd=str(repo),
            env={**os.environ, **env},
            capture_output=True,
            check=False,
        )
        raw_proc = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", "--cached", "--raw", "--no-renames", "-z", base_commit],
            cwd=str(repo),
            env={**os.environ, **env},
            capture_output=True,
            check=False,
        )
        patch_proc = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "core.quotePath=false", "diff", "--cached", "--binary", base_commit],
            cwd=str(repo),
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    ns_entries = parse_name_status_z(ns_proc.stdout)
    raw_entries = parse_raw_diff_z(raw_proc.stdout)

    symlink_paths: list[str] = []
    submodule_paths: list[str] = []
    for entry in raw_entries:
        if entry.new_mode == "120000" or entry.old_mode == "120000":
            symlink_paths.append(entry.path)
        if entry.new_mode == "160000" or entry.old_mode == "160000":
            submodule_paths.append(entry.path)
    for rel in untracked:
        candidate = repo / posixpath.normpath(rel)
        if os.path.islink(candidate):
            symlink_paths.append(rel)

    changed_paths = sorted({e.path for e in ns_entries} | {e.path for e in raw_entries} | set(untracked))
    patch_text = patch_proc.stdout
    patch_sha = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()

    return DiffBundle(
        base_commit=base_commit,
        patch_text=patch_text,
        patch_sha256=patch_sha,
        name_status=tuple(ns_entries),
        raw_entries=tuple(raw_entries),
        untracked_files=tuple(sorted(untracked)),
        symlink_paths=tuple(sorted(set(symlink_paths))),
        submodule_paths=tuple(sorted(set(submodule_paths))),
        changed_paths=tuple(changed_paths),
    )


def apply_patch_cleanly(worktree_dir: Path, patch_text: str) -> None:
    """Apply a unified diff patch strictly inside `worktree_dir`."""
    if not patch_text.strip():
        return
    with tempfile.NamedTemporaryFile("wb", suffix=".patch", delete=False) as fp:
        fp.write(patch_text.encode("utf-8"))
        patch_path = Path(fp.name)
    try:
        run_git(["apply", "--check", str(patch_path)], cwd=worktree_dir)
        run_git(["apply", str(patch_path)], cwd=worktree_dir)
    finally:
        patch_path.unlink(missing_ok=True)


def get_remote_branch_tip(remote_url: str, branch: str) -> str | None:
    proc = run_git(["ls-remote", remote_url, f"refs/heads/{branch}"])
    line = proc.stdout.strip().splitlines()
    if not line:
        return None
    return line[0].split()[0]


def commit_and_push_branch(
    worktree_dir: Path,
    *,
    remote_url: str,
    branch_name: str,
    target_branch: str,
    expected_base_sha: str,
    commit_message: str,
    patch_text: str | None = None,
) -> dict[str, Any]:
    """Commit staged/applied patch onto `branch_name` and push if `target_branch` tip matches baseline."""
    tip = get_remote_branch_tip(remote_url, target_branch)
    if tip is not None and tip != expected_base_sha:
        raise BaselineStaleError(
            f"Target branch {target_branch!r} moved from {expected_base_sha[:12]} to {tip[:12]}; re-verify before publishing.",
            details={"expected_base_sha": expected_base_sha, "remote_tip_sha": tip},
        )

    repo = Path(worktree_dir).resolve()
    run_git(["config", "user.email", "sdlc-bot@ai-sdlc.local"], cwd=repo)
    run_git(["config", "user.name", "AI SDLC Publisher"], cwd=repo)
    run_git(["checkout", "-B", branch_name, expected_base_sha], cwd=repo)

    if patch_text:
        apply_patch_cleanly(repo, patch_text)

    # Filter out ignored transient artifacts before staging.
    run_git(["add", "-A"], cwd=repo)
    for ignored in IGNORED_UPLOAD_DIRS:
        run_git(["reset", "--", ignored], cwd=repo) if (repo / ignored).exists() else None

    status_out = run_git(["status", "--porcelain"], cwd=repo).stdout.strip()
    if not status_out:
        head_sha = run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
        return {"branch": branch_name, "commit_sha": head_sha, "pushed": False}

    run_git(["commit", "-m", commit_message], cwd=repo)
    head_sha = run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    run_git(["push", remote_url, f"HEAD:refs/heads/{branch_name}", "--force"], cwd=repo)
    return {"branch": branch_name, "commit_sha": head_sha, "pushed": True}
