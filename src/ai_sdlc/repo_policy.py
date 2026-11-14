"""Shared repository path classification and secret-safe file collection policies."""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
from collections.abc import Callable, Iterable
from pathlib import Path

PROTECTED_EXACT_PATHS: frozenset[str] = frozenset(
    {
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "tox.ini",
        "conftest.py",
        "setup.py",
        "uv.lock",
        "poetry.lock",
        ".gitlab-ci.yml",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "tsconfig.json",
        "tsconfig.base.json",
        "vitest.config.ts",
        "vitest.config.js",
        "vitest.config.mts",
        "jest.config.ts",
        "jest.config.js",
        "jest.config.cjs",
        "eslint.config.js",
        "eslint.config.mjs",
        ".eslintrc.json",
        ".eslintrc.js",
    }
)

PROTECTED_PREFIXES: tuple[str, ...] = (
    ".github/",
    ".circleci/",
    "ci/",
)

TEST_FILE_RE = re.compile(r"^(?:test_.+|.+_test)\.py$")
JS_TS_TEST_FILE_RE = re.compile(r"^.+\.(?:test|spec)\.(?:ts|tsx|js|jsx|mjs|cjs)$")

IGNORED_UPLOAD_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        ".eggs",
        "node_modules",
        "dist",
        "coverage",
        ".next",
        ".turbo",
        "artifacts",
        ".aws",
        ".ssh",
        ".gnupg",
        ".kube",
    }
)

SAFE_EXAMPLE_FILENAMES: frozenset[str] = frozenset(
    {
        ".env.example",
        ".env.sample",
        ".env.template",
        "models.local.example.json",
    }
)

SECRET_EXACT_FILENAMES: frozenset[str] = frozenset(
    {
        ".npmrc",
        ".pypirc",
        ".netrc",
        "_netrc",
        ".git-credentials",
        ".pgpass",
        ".htpasswd",
        ".dockercfg",
        ".secrets",
        "kubeconfig",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)

SECRET_FILE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".kdbx",
        ".jks",
        ".keystore",
        ".ppk",
        ".ovpn",
        ".der",
    }
)

SECRET_GLOB_PATTERNS: tuple[str, ...] = (
    "*credentials*.json",
    "*credentials*.yaml",
    "*credentials*.yml",
    "*service_account*.json",
    "*service-account*.json",
    "secrets.*",
    "*.secrets.*",
    "*_secrets.json",
    "*_secrets.yaml",
    "*_secrets.yml",
    ".secret*",
)


def normalize_repo_rel_path(path: str) -> str:
    """Normalize a repository-relative POSIX path and reject traversal/absolute paths."""
    if not isinstance(path, str):
        raise ValueError(f"Path must be str, got {type(path).__name__}")
    if "\x00" in path:
        raise ValueError("Path cannot contain NUL bytes")
    cleaned = path.replace("\\", "/").strip()
    if not cleaned:
        raise ValueError("Path cannot be empty")
    if cleaned.startswith("/") or (len(cleaned) >= 2 and cleaned[1] == ":"):
        raise ValueError(f"Expected repo-relative path, got {path!r}")
    norm = posixpath.normpath(cleaned)
    if norm in ("", ".") or norm == ".." or norm.startswith("../"):
        raise ValueError(f"Invalid repo-relative path traversal: {path!r}")
    return norm


def is_protected_config_path(path: str) -> bool:
    """Return True if `path` belongs to protected build/test/CI configuration files.

    `conftest.py` is protected at every directory depth (`conftest.py`, `tests/conftest.py`,
    etc.) because nested `conftest.py` files can alter pytest collection, hooks, or results.
    """
    norm = normalize_repo_rel_path(path)
    if norm in PROTECTED_EXACT_PATHS:
        return True
    base = posixpath.basename(norm)
    if base == "conftest.py":
        return True
    if any(norm.startswith(prefix) for prefix in PROTECTED_PREFIXES):
        return True
    if fnmatch.fnmatch(norm, "requirements*.txt") or norm.startswith("requirements/") and base.endswith(".txt"):
        return True
    return False


def is_test_path(path: str) -> bool:
    """Return True if `path` is a Python or JS/TS test module/helper."""
    norm = normalize_repo_rel_path(path)
    if is_protected_config_path(norm):
        return False
    parts = norm.split("/")
    base = parts[-1]
    if norm.endswith(".py"):
        if any(segment in ("tests", "test") for segment in parts[:-1]):
            return True
        return bool(TEST_FILE_RE.match(base))
    if JS_TS_TEST_FILE_RE.match(base):
        return True
    if any(segment in ("__tests__", "tests", "test") for segment in parts[:-1]) and base.endswith(
        (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
    ):
        return True
    return False


def is_valid_new_test_file_path(path: str) -> bool:
    """Return True if `path` satisfies new-test-file naming rules for Python or JS/TS."""
    norm = normalize_repo_rel_path(path)
    if not is_test_path(norm):
        return False
    base = posixpath.basename(norm)
    return bool(TEST_FILE_RE.match(base) or JS_TS_TEST_FILE_RE.match(base))


def collect_existing_test_files(repo_files: Iterable[str]) -> tuple[str, ...]:
    """Return sorted existing test files present at `base_commit_sha`."""
    out: set[str] = set()
    for raw in repo_files:
        norm = normalize_repo_rel_path(raw)
        if is_test_path(norm):
            out.add(norm)
    return tuple(sorted(out))


def collect_protected_config_paths(repo_files: Iterable[str]) -> tuple[str, ...]:
    """Return sorted protected config paths present in `repo_files`."""
    out: set[str] = set()
    for raw in repo_files:
        norm = normalize_repo_rel_path(raw)
        if is_protected_config_path(norm):
            out.add(norm)
    return tuple(sorted(out))


def upload_block_reason(repo_root: Path, path_obj: Path, rel_posix: str) -> str | None:
    """Return a block reason BEFORE reading file bytes, or None if safe to upload/inspect."""
    if path_obj.is_symlink():
        return "symlink"
    try:
        resolved = path_obj.resolve(strict=True)
    except OSError:
        return "unresolvable_path"
    if not resolved.is_relative_to(repo_root):
        return "escapes_repo_root"
    if not path_obj.is_file():
        return "not_regular_file"

    parts_lower = [part.lower() for part in rel_posix.split("/")]
    if any(seg in IGNORED_UPLOAD_DIRS for seg in parts_lower[:-1]):
        return "ignored_directory"

    name_lower = parts_lower[-1]
    if name_lower in SAFE_EXAMPLE_FILENAMES:
        return None
    if name_lower == ".env" or name_lower.startswith(".env."):
        return "env_file"
    if name_lower.endswith(".local.json"):
        return "local_json_override"
    if name_lower in SECRET_EXACT_FILENAMES:
        return "credential_file"
    if re.match(r"^id_(?:rsa|dsa|ecdsa|ed25519)\b", name_lower) and not name_lower.endswith(".pub"):
        return "private_key"
    if posixpath.splitext(name_lower)[1] in SECRET_FILE_SUFFIXES:
        return "key_material"
    if any(fnmatch.fnmatch(name_lower, pat) for pat in SECRET_GLOB_PATTERNS):
        return "secret_pattern"
    return None


def collect_safe_repo_files(
    repo_dir: Path,
    *,
    on_skip_dir: Callable[[str], None] | None = None,
    on_skip_file: Callable[[str, str], None] | None = None,
) -> list[tuple[str, bytes]]:
    """Walk `repo_dir` without following symlinks and read only non-secret, non-ignored files."""
    repo_root = repo_dir.resolve(strict=True)
    files: list[tuple[str, bytes]] = []
    for root, dirs, filenames in os.walk(repo_root, followlinks=False):
        root_path = Path(root)
        kept_dirs: list[str] = []
        for d in sorted(dirs):
            dir_path = root_path / d
            if d.lower() in IGNORED_UPLOAD_DIRS:
                continue
            if dir_path.is_symlink():
                if on_skip_dir is not None:
                    on_skip_dir(dir_path.relative_to(repo_root).as_posix())
                continue
            try:
                if not dir_path.resolve(strict=True).is_relative_to(repo_root):
                    continue
            except OSError:
                continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs

        for fname in sorted(filenames):
            if fname.lower().endswith((".pyc", ".pyo")):
                continue
            full = root_path / fname
            rel = full.relative_to(repo_root).as_posix()
            reason = upload_block_reason(repo_root, full, rel)
            if reason is not None:
                if on_skip_file is not None:
                    on_skip_file(rel, reason)
                continue
            files.append((rel, full.read_bytes()))
    return files
