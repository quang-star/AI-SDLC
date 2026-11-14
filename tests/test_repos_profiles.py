"""P3 tests: real git onboarding, support matrix profiles, templates, GitHub webhooks, and diff parsing."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from ai_sdlc_saas.api.app import create_app
from ai_sdlc_saas.config import Settings
from ai_sdlc_saas.repos.git_ops import clone_at_commit, collect_git_diff_bundle, init_bare_repo_from_dir
from ai_sdlc_saas.security.crypto import compute_github_hmac_sha256


def _setup_client(tmp_path: Path) -> tuple[TestClient, Settings]:
    settings = Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path / 'repos.db'}",
        object_store_dir=str(tmp_path / "objects"),
        worktree_root=str(tmp_path / "worktrees"),
        jwt_secret="test-jwt-secret-key-min-32-chars!!",
        encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        github_webhook_secret="gh-webhook-secret",
        stripe_webhook_secret="st-sec",
        allow_local_git_repos=True,
    )
    return TestClient(create_app(settings)), settings


def _register_org(client: TestClient, email: str, org_name: str) -> str:
    res = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Password123!", "full_name": "Dev", "org_name": org_name},
    )
    assert res.status_code == 201
    return str(res.json()["access_token"])


def test_python_ts_multi_repo_templates_and_unsupported_rejection(tmp_path: Path) -> None:
    client, _ = _setup_client(tmp_path)
    tok = _register_org(client, "lead@acme.com", "Acme Eng")
    headers = {"Authorization": f"Bearer {tok}"}

    # 1. Create a project holding both Python + TS repos
    proj = client.post("/api/v1/projects", headers=headers, json={"name": "Core Platform"})
    assert proj.status_code == 201
    proj_id = proj.json()["id"]

    # 2. Scaffold python-fastapi-starter and run real pytest baseline
    py_tpl = client.post(
        "/api/v1/repos/from-template",
        headers=headers,
        json={"project_id": proj_id, "template_id": "python-fastapi-starter", "repo_name": "api-svc"},
    )
    assert py_tpl.status_code == 201, py_tpl.text
    py_data = py_tpl.json()
    assert py_data["support_profile"] == "python-pytest"
    assert py_data["baseline"]["executed"] is True
    assert py_data["baseline"]["passed"] is True

    # 3. Scaffold node-ts-vite-starter (multi-repo same project)
    ts_tpl = client.post(
        "/api/v1/repos/from-template",
        headers=headers,
        json={"project_id": proj_id, "template_id": "node-ts-vite-starter", "repo_name": "web-ui"},
    )
    assert ts_tpl.status_code == 201, ts_tpl.text
    assert ts_tpl.json()["support_profile"] == "node-ts-vitest"

    repos = client.get(f"/api/v1/repos?project_id={proj_id}", headers=headers)
    assert repos.status_code == 200
    assert len(repos.json()) == 2

    # Verify RepoContext persisted in ObjectStore
    ctx_resp = client.get(f"/api/v1/repos/{py_data['id']}/context", headers=headers)
    assert ctx_resp.status_code == 200
    assert ctx_resp.json()["repo_context"]["language"] == "python"

    # 4. Unsupported Rust repo explicitly rejected pre-onboarding
    rust_src = tmp_path / "rust_src"
    rust_src.mkdir()
    (rust_src / "Cargo.toml").write_text('[package]\nname = "rs"\nversion = "0.1.0"\n', encoding="utf-8")
    (rust_src / "src").mkdir()
    (rust_src / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    rust_bare = tmp_path / "rust.git"
    init_bare_repo_from_dir(rust_src, rust_bare)

    rej = client.post(
        "/api/v1/repos",
        headers=headers,
        json={
            "project_id": proj_id,
            "provider": "local_git",
            "full_name": "acme/rust-worker",
            "clone_url_or_path": str(rust_bare),
        },
    )
    assert rej.status_code == 422
    assert rej.json()["error"]["code"] == "unsupported_profile"

    # 5. Cross-tenant isolation: Tenant B cannot inspect Tenant A repo context
    tok_b = _register_org(client, "other@beta.com", "Beta Inc")
    assert client.get(f"/api/v1/repos/{py_data['id']}/context", headers={"Authorization": f"Bearer {tok_b}"}).status_code == 404


def test_github_installation_binding_and_webhook_revocation(tmp_path: Path) -> None:
    client, settings = _setup_client(tmp_path)
    tok = _register_org(client, "owner@gh.com", "GH Org")
    headers = {"Authorization": f"Bearer {tok}"}

    proj_id = client.post("/api/v1/projects", headers=headers, json={"name": "App"}).json()["id"]
    inst = client.post(
        "/api/v1/installations/github",
        headers=headers,
        json={
            "external_installation_id": "998877",
            "account_login": "acme-gh",
            "ownership_proof": "proof-secret-token",
            "access_token": "ghs_secret_install_token",
        },
    )
    assert inst.status_code == 201
    inst_id = inst.json()["id"]

    # Create local bare repo simulating installation-backed clone
    py_src = tmp_path / "py_src"
    py_src.mkdir()
    (py_src / "app.py").write_text("def hi(): return 'ok'\n", encoding="utf-8")
    (py_src / "test_app.py").write_text("from app import hi\ndef test_hi(): assert hi() == 'ok'\n", encoding="utf-8")
    py_bare = tmp_path / "py.git"
    init_bare_repo_from_dir(py_src, py_bare)

    conn = client.post(
        "/api/v1/repos",
        headers=headers,
        json={
            "project_id": proj_id,
            "provider": "github_cloud",
            "full_name": "acme-gh/service",
            "clone_url_or_path": str(py_bare),
            "installation_id": inst_id,
        },
    )
    assert conn.status_code == 201
    repo_id = conn.json()["id"]

    # Send invalid HMAC webhook -> 401
    bad_payload = json.dumps({"action": "deleted", "installation": {"id": "998877"}}).encode("utf-8")
    assert (
        client.post(
            "/api/v1/webhooks/github",
            content=bad_payload,
            headers={"X-GitHub-Event": "installation", "X-GitHub-Delivery": "d1", "X-Hub-Signature-256": "sha256=deadbeef"},
        ).status_code
        == 401
    )

    # Valid installation.deleted webhook disconnects linked repo immediately
    sig = compute_github_hmac_sha256(bad_payload, settings.github_webhook_secret)
    wh = client.post(
        "/api/v1/webhooks/github",
        content=bad_payload,
        headers={"X-GitHub-Event": "installation", "X-GitHub-Delivery": "d1", "X-Hub-Signature-256": sig},
    )
    assert wh.status_code == 200
    assert wh.json()["status"] == "processed"

    # Duplicate delivery ignored idempotently
    wh_dup = client.post(
        "/api/v1/webhooks/github",
        content=bad_payload,
        headers={"X-GitHub-Event": "installation", "X-GitHub-Delivery": "d1", "X-Hub-Signature-256": sig},
    )
    assert wh_dup.json()["status"] == "duplicate_ignored"

    # Repo status is disconnected & refresh fails
    repos = client.get("/api/v1/repos", headers=headers).json()
    assert repos[0]["status"] == "disconnected"
    assert client.post(f"/api/v1/repos/{repo_id}/refresh", headers=headers).status_code == 409


def test_git_diff_bundle_detects_rename_and_new_test(tmp_path: Path) -> None:
    src = tmp_path / "src_repo"
    src.mkdir()
    (src / "calc.py").write_text("def mul(a, b):\n    return a + b\n", encoding="utf-8")
    (src / "old_name.py").write_text("X = 1\n", encoding="utf-8")
    bare = tmp_path / "calc.git"
    base_sha = init_bare_repo_from_dir(src, bare)

    wt = tmp_path / "wt"
    clone_at_commit(str(bare), wt, branch_or_sha=base_sha, allow_local_repos=True)
    (wt / "calc.py").write_text("def mul(a, b):\n    return a * b\n", encoding="utf-8")
    (wt / "old_name.py").rename(wt / "new_name.py")
    (wt / "tests").mkdir()
    (wt / "tests" / "test_calc.py").write_text("from calc import mul\ndef test_mul(): assert mul(2, 3) == 6\n", encoding="utf-8")

    bundle = collect_git_diff_bundle(wt, base_sha)
    assert bundle.base_commit == base_sha
    assert "calc.py" in bundle.changed_paths
    assert "tests/test_calc.py" in bundle.untracked_files
    assert "def mul(a, b):" in bundle.patch_text
    assert len(bundle.patch_sha256) == 64
    assert bundle.symlink_paths == ()
    assert bundle.submodule_paths == ()
