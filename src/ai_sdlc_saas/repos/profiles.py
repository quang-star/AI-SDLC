"""Support Matrix classification and repository pre-flight environment validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_sdlc.contracts import RepoContext
from ai_sdlc.repo_context import RepoLimitExceededError, build_repo_context
from ai_sdlc_saas.errors import UnsupportedProfileError

SUPPORTED_PROFILES: tuple[str, ...] = (
    "python-pytest",
    "node-ts-vitest",
    "node-js-jest",
    "docs-only",
)

_UNSUPPORTED_MARKERS: dict[str, str] = {
    "Cargo.toml": "Rust/Cargo is outside the GA official Support Matrix.",
    "go.mod": "Go modules are outside the GA official Support Matrix.",
    "pom.xml": "Java Maven is outside the GA official Support Matrix.",
    "build.gradle": "Gradle/JVM is outside the GA official Support Matrix.",
    "Gemfile": "Ruby Bundler is outside the GA official Support Matrix.",
    "Package.swift": "SwiftPM is outside the GA official Support Matrix.",
    "CMakeLists.txt": "Native C/C++ CMake builds are outside the GA official Support Matrix.",
}


@dataclass(frozen=True)
class ProfileInspection:
    supported: bool
    profile_id: str
    install_cmd: str
    test_cmd: str
    lint_cmd: str
    typecheck_cmd: str
    build_cmd: str
    reasons: tuple[str, ...]
    repo_context: RepoContext


def inspect_repo_profile(repo_dir: Path, *, repo_source: str | None = None) -> ProfileInspection:
    """Inspect `repo_dir` against the GA Support Matrix and build grounded `RepoContext`."""
    root = Path(repo_dir).resolve()
    try:
        ctx = build_repo_context(root, repo_source=repo_source)
    except RepoLimitExceededError as exc:
        raise UnsupportedProfileError(str(exc), details={"reason": "repo_limit_exceeded"}) from exc

    tracked_set = set(ctx.tracked_files)
    top_names = {p.split("/")[0] for p in tracked_set}
    unsupported_reasons: list[str] = []
    for marker, reason in _UNSUPPORTED_MARKERS.items():
        if marker in tracked_set or marker in top_names:
            unsupported_reasons.append(reason)

    framework = ctx.test_runner.framework
    lang = ctx.language

    if unsupported_reasons and lang in ("unknown", "docs"):
        raise UnsupportedProfileError(
            "; ".join(unsupported_reasons),
            details={"tracked_files_count": len(ctx.tracked_files), "language": lang},
        )

    if lang == "python" or (lang == "multi" and framework == "pytest"):
        return ProfileInspection(
            supported=True,
            profile_id="python-pytest",
            install_cmd="pip install -e .",
            test_cmd="python -m pytest -q",
            lint_cmd="python -m ruff check .",
            typecheck_cmd="python -m mypy src" if (root / "src").is_dir() else "",
            build_cmd="",
            reasons=(),
            repo_context=ctx,
        )

    if lang in ("typescript", "multi") and framework == "vitest":
        return ProfileInspection(
            supported=True,
            profile_id="node-ts-vitest",
            install_cmd="npm ci",
            test_cmd="npx vitest run",
            lint_cmd="",
            typecheck_cmd="npx tsc --noEmit",
            build_cmd="npm run build",
            reasons=(),
            repo_context=ctx,
        )

    if lang in ("javascript", "typescript", "multi") and framework in ("jest", "npm-test", "mocha"):
        return ProfileInspection(
            supported=True,
            profile_id="node-js-jest" if framework == "jest" else "node-ts-vitest",
            install_cmd="npm ci",
            test_cmd=ctx.test_runner.command_hint or "npm test",
            lint_cmd="",
            typecheck_cmd="npx tsc --noEmit" if lang in ("typescript", "multi") else "",
            build_cmd="",
            reasons=(),
            repo_context=ctx,
        )

    if lang in ("typescript", "javascript") and "package.json" in tracked_set:
        return ProfileInspection(
            supported=True,
            profile_id="node-ts-vitest" if lang == "typescript" else "node-js-jest",
            install_cmd="npm ci",
            test_cmd=ctx.test_runner.command_hint or "npm test",
            lint_cmd="",
            typecheck_cmd="npx tsc --noEmit" if lang == "typescript" else "",
            build_cmd="",
            reasons=(),
            repo_context=ctx,
        )

    if lang == "docs":
        return ProfileInspection(
            supported=True,
            profile_id="docs-only",
            install_cmd="",
            test_cmd="",
            lint_cmd="",
            typecheck_cmd="",
            build_cmd="",
            reasons=(),
            repo_context=ctx,
        )

    reasons = tuple(unsupported_reasons) or (
        f"Language {lang!r} with framework {framework!r} is not supported in GA Support Matrix ({', '.join(SUPPORTED_PROFILES)}).",
    )
    raise UnsupportedProfileError(
        " ".join(reasons),
        details={"language": lang, "framework": framework, "supported_profiles": list(SUPPORTED_PROFILES)},
    )


def profile_to_dict(inspection: ProfileInspection) -> dict[str, Any]:
    return {
        "supported": inspection.supported,
        "profile_id": inspection.profile_id,
        "install_cmd": inspection.install_cmd,
        "test_cmd": inspection.test_cmd,
        "lint_cmd": inspection.lint_cmd,
        "typecheck_cmd": inspection.typecheck_cmd,
        "build_cmd": inspection.build_cmd,
        "reasons": list(inspection.reasons),
        "commit_sha": inspection.repo_context.repo_commit_sha,
        "tree_sha256": inspection.repo_context.repo_tree_sha256,
        "language": inspection.repo_context.language,
        "tracked_file_count": len(inspection.repo_context.tracked_files),
    }
