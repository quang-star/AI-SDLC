"""Build a grounded, secret-safe `RepoContext` from a local repository or fixture directory."""

from __future__ import annotations

import ast
import json
import posixpath
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Literal

from ai_sdlc.contracts import (
    CommitSource,
    FileRole,
    RepoContext,
    RepoFileSummary,
    SymbolInfo,
    SymbolKind,
    TestRunnerInfo,
    canonical_json_bytes,
    sha256_hex,
    utc_now_iso,
)
from ai_sdlc.repo_policy import (
    collect_existing_test_files,
    collect_protected_config_paths,
    collect_safe_repo_files,
    is_protected_config_path,
    is_test_path,
)

_MAX_FILE_EXCERPT_CHARS = 4_000
DEFAULT_MAX_TRACKED_FILES = 2_500
DEFAULT_MAX_REPO_BYTES = 50 * 1024 * 1024

_JS_TS_EXPORT_RE = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:(async\s+)?function\s+([A-Za-z_$][\w$]*)"
    r"|class\s+([A-Za-z_$][\w$]*)"
    r"|interface\s+([A-Za-z_$][\w$]*)"
    r"|type\s+([A-Za-z_$][\w$]*)"
    r"|const\s+([A-Za-z_$][\w$]*))"
)


class RepoLimitExceededError(ValueError):
    """Raised when a repository exceeds support-matrix file count or byte limits."""


def _classify_file_role(rel_path: str) -> FileRole:
    if is_protected_config_path(rel_path):
        return "config"
    if is_test_path(rel_path):
        return "test"
    lower = rel_path.lower()
    if lower.endswith((".md", ".rst", ".txt")) or lower.startswith("docs/"):
        return "docs"
    if lower.endswith((".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java")):
        return "source"
    return "other"


def _first_doc_line(node: ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef | ast.Module) -> str:
    doc = ast.get_docstring(node, clean=True)
    if not doc:
        return ""
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:160]
    return ""


def _format_fn_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, *, prefix: str = "") -> str:
    args = ast.unparse(node.args)
    ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    async_kw = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    return f"{async_kw}def {prefix}{node.name}({args}){ret}"


def _extract_python_symbols(source_text: str) -> tuple[SymbolInfo, ...]:
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return ()

    symbols: list[SymbolInfo] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            sig = f"class {node.name}({bases})" if bases else f"class {node.name}"
            symbols.append(
                SymbolInfo(
                    name=node.name,
                    kind="class",
                    signature=sig,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    docstring_summary=_first_doc_line(node),
                )
            )
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind: SymbolKind = "async_method" if isinstance(child, ast.AsyncFunctionDef) else "method"
                    symbols.append(
                        SymbolInfo(
                            name=f"{node.name}.{child.name}",
                            kind=kind,
                            signature=_format_fn_signature(child, prefix=f"{node.name}."),
                            line_start=child.lineno,
                            line_end=child.end_lineno or child.lineno,
                            docstring_summary=_first_doc_line(child),
                        )
                    )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            symbols.append(
                SymbolInfo(
                    name=node.name,
                    kind=kind,
                    signature=_format_fn_signature(node),
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    docstring_summary=_first_doc_line(node),
                )
            )
    return tuple(symbols)


def _extract_js_ts_symbols(source_text: str) -> tuple[SymbolInfo, ...]:
    symbols: list[SymbolInfo] = []
    for lineno, line in enumerate(source_text.splitlines(), start=1):
        match = _JS_TS_EXPORT_RE.match(line)
        if not match:
            continue
        is_async, fn_name, cls_name, iface_name, type_name, const_name = match.groups()
        if fn_name:
            kind: SymbolKind = "async_function" if is_async else "function"
            name = fn_name
        elif cls_name:
            kind = "class"
            name = cls_name
        elif iface_name or type_name:
            kind = "class"
            name = iface_name or type_name
        elif const_name:
            kind = "function"
            name = const_name
        else:
            continue
        symbols.append(
            SymbolInfo(
                name=name,
                kind=kind,
                signature=line.strip()[:160],
                line_start=lineno,
                line_end=lineno,
            )
        )
    return tuple(symbols)


def _decode_text_bounded(raw: bytes, *, max_chars: int) -> tuple[int, str]:
    if b"\x00" in raw[:4096]:
        return 0, ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return 0, ""
    line_count = len(text.splitlines())
    if max_chars <= 0:
        return line_count, ""
    if len(text) <= max_chars:
        return line_count, text
    return line_count, text[:max_chars].rstrip() + "\n... [truncated]"


def _detect_git_anchor(repo_root: Path, tree_sha256: str) -> tuple[str, CommitSource, bool]:
    """Read HEAD SHA and dirty flag only if `repo_root` is itself a git checkout."""
    if not (repo_root / ".git").exists():
        return f"tree-{tree_sha256[:12]}", "tree_snapshot", False

    rev = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if rev.returncode != 0 or not rev.stdout.strip():
        return f"tree-{tree_sha256[:12]}", "tree_snapshot", False

    status = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain", "-uall"],
        capture_output=True,
        text=True,
        check=False,
    )
    is_dirty = bool(status.returncode == 0 and status.stdout.strip())
    return rev.stdout.strip(), "git_head", is_dirty


def _extract_project_and_runner(files_by_path: dict[str, bytes], repo_root: Path) -> tuple[str, TestRunnerInfo]:
    repo_name = repo_root.name or "repo"
    pythonpath: tuple[str, ...] = ()
    testpaths: tuple[str, ...] = ()
    config_file: str | None = None

    if "pyproject.toml" in files_by_path:
        config_file = "pyproject.toml"
        try:
            data = tomllib.loads(files_by_path["pyproject.toml"].decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            data = {}
        project = data.get("project")
        if isinstance(project, dict) and isinstance(project.get("name"), str) and project["name"].strip():
            repo_name = project["name"].strip()
        pytest_opts = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
        if isinstance(pytest_opts, dict):
            raw_pp = pytest_opts.get("pythonpath")
            if isinstance(raw_pp, list):
                pythonpath = tuple(str(x) for x in raw_pp if str(x).strip())
            raw_tp = pytest_opts.get("testpaths")
            if isinstance(raw_tp, list):
                testpaths = tuple(str(x) for x in raw_tp if str(x).strip())
        return repo_name, TestRunnerInfo(
            framework="pytest",
            config_file=config_file,
            pythonpath=pythonpath,
            testpaths=testpaths,
            command_hint="pytest -q",
        )
    if "pytest.ini" in files_by_path:
        return repo_name, TestRunnerInfo(
            framework="pytest",
            config_file="pytest.ini",
            pythonpath=(),
            testpaths=(),
            command_hint="pytest -q",
        )
    if "package.json" in files_by_path:
        try:
            pkg = json.loads(files_by_path["package.json"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pkg = {}
        if isinstance(pkg, dict):
            if isinstance(pkg.get("name"), str) and pkg["name"].strip():
                repo_name = pkg["name"].strip()
            raw_scripts = pkg.get("scripts")
            scripts: dict[str, object] = raw_scripts if isinstance(raw_scripts, dict) else {}
            raw_deps = pkg.get("dependencies")
            raw_dev_deps = pkg.get("devDependencies")
            deps: dict[str, object] = {}
            if isinstance(raw_deps, dict):
                deps.update(raw_deps)
            if isinstance(raw_dev_deps, dict):
                deps.update(raw_dev_deps)
            test_script = str(scripts.get("test") or "")
            if "vitest" in deps or "vitest" in test_script:
                framework = "vitest"
                hint = "npx vitest run"
            elif "jest" in deps or "jest" in test_script:
                framework = "jest"
                hint = "npx jest --ci"
            else:
                framework = "npm"
                hint = "npm test"
            return repo_name, TestRunnerInfo(
                framework=framework,
                config_file="package.json",
                pythonpath=(),
                testpaths=(),
                command_hint=hint,
            )

    return repo_name, TestRunnerInfo(
        framework="pytest",
        config_file=config_file,
        pythonpath=pythonpath,
        testpaths=testpaths,
        command_hint="pytest -q",
    )


def _priority_key(path: str, role: FileRole) -> tuple[int, int, str]:
    order: dict[FileRole, int] = {
        "source": 0,
        "test": 1,
        "config": 2,
        "docs": 3,
        "other": 4,
    }
    # Put __init__.py after substantive modules within source/test.
    is_init = 1 if posixpath.basename(path) == "__init__.py" else 0
    return (order[role], is_init, path)


def build_repo_context(
    repo_dir: Path,
    *,
    repo_source: str | None = None,
    max_excerpt_chars: int = 24_000,
    max_tracked_files: int = DEFAULT_MAX_TRACKED_FILES,
    max_repo_bytes: int = DEFAULT_MAX_REPO_BYTES,
    captured_at: str | None = None,
) -> RepoContext:
    """Inspect `repo_dir` safely and build a commit/tree-anchored `RepoContext`."""
    repo_root = Path(repo_dir).resolve(strict=True)
    if not repo_root.is_dir():
        raise ValueError(f"`repo_dir` must be an existing directory: {repo_dir}")

    safe_files = collect_safe_repo_files(repo_root)
    if not safe_files:
        raise ValueError(f"No safe inspectable files found in {repo_dir}")
    if len(safe_files) > max_tracked_files:
        raise RepoLimitExceededError(
            f"Repository tracked file count ({len(safe_files)}) exceeds limit ({max_tracked_files})."
        )
    total_bytes = sum(len(raw) for _, raw in safe_files)
    if total_bytes > max_repo_bytes:
        raise RepoLimitExceededError(
            f"Repository payload size ({total_bytes} bytes) exceeds limit ({max_repo_bytes} bytes)."
        )

    files_by_path = dict(sorted(safe_files, key=lambda item: item[0]))
    file_hashes = {rel: sha256_hex(raw) for rel, raw in files_by_path.items()}
    repo_tree_sha256 = sha256_hex(
        canonical_json_bytes([[rel, file_hashes[rel]] for rel in sorted(file_hashes)])
    )

    repo_commit_sha, commit_source, is_dirty = _detect_git_anchor(repo_root, repo_tree_sha256)
    tracked_files = tuple(sorted(files_by_path))
    existing_test_files = collect_existing_test_files(tracked_files)
    protected_config_paths = collect_protected_config_paths(tracked_files)
    protected_config_hashes = {p: file_hashes[p] for p in protected_config_paths}

    repo_name, test_runner = _extract_project_and_runner(files_by_path, repo_root)
    has_py = any(p.endswith(".py") for p in tracked_files)
    has_ts = any(p.endswith((".ts", ".tsx")) for p in tracked_files)
    has_js = any(p.endswith((".js", ".jsx", ".mjs", ".cjs")) for p in tracked_files)
    if has_py and (has_ts or has_js):
        language: Literal["python"] | str = "multi"
    elif has_py:
        language = "python"
    elif has_ts:
        language = "typescript"
    elif has_js:
        language = "javascript"
    elif any(p.endswith((".md", ".rst")) for p in tracked_files):
        language = "docs"
    else:
        language = "unknown"

    remaining_budget = max(0, max_excerpt_chars)
    summaries: list[RepoFileSummary] = []
    for rel in sorted(tracked_files, key=lambda p: _priority_key(p, _classify_file_role(p))):
        raw = files_by_path[rel]
        role = _classify_file_role(rel)
        per_file_cap = min(_MAX_FILE_EXCERPT_CHARS, remaining_budget) if remaining_budget > 0 else 0
        line_count, excerpt = _decode_text_bounded(raw, max_chars=max(per_file_cap, 0))
        if per_file_cap == 0:
            # Still compute line count & AST symbols even if excerpt budget is exhausted.
            line_count, _ = _decode_text_bounded(raw, max_chars=0)
            excerpt = ""
        else:
            remaining_budget = max(0, remaining_budget - len(excerpt))

        symbols: tuple[SymbolInfo, ...] = ()
        if rel.endswith(".py") and b"\x00" not in raw[:4096]:
            try:
                symbols = _extract_python_symbols(raw.decode("utf-8"))
            except UnicodeDecodeError:
                symbols = ()
        elif rel.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs")) and b"\x00" not in raw[:4096]:
            try:
                symbols = _extract_js_ts_symbols(raw.decode("utf-8"))
            except UnicodeDecodeError:
                symbols = ()

        summaries.append(
            RepoFileSummary(
                path=rel,
                role=role,
                sha256=file_hashes[rel],
                line_count=line_count,
                symbols=symbols,
                excerpt=excerpt,
            )
        )

    context_id = f"ctx-{repo_tree_sha256[:12]}"
    return RepoContext(
        context_id=context_id,
        captured_at=captured_at or utc_now_iso(),
        repo_name=repo_name,
        repo_source=repo_source or repo_root.name,
        repo_commit_sha=repo_commit_sha,
        commit_source=commit_source,
        repo_tree_sha256=repo_tree_sha256,
        is_dirty=is_dirty,
        language=language,
        tracked_files=tracked_files,
        existing_test_files=existing_test_files,
        protected_config_paths=protected_config_paths,
        protected_config_hashes=protected_config_hashes,
        test_runner=test_runner,
        file_contexts=tuple(summaries),
    )
