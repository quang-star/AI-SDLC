"""P11 Playwright browser end-to-end test against FastAPI + compiled React+Vite UI."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from ai_sdlc_saas.api.app import create_app
from ai_sdlc_saas.config import Settings
from ai_sdlc_saas.gateway.adapters import DeterministicMockAdapter, GatewayResponse

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIST_DIR = REPO_ROOT / "apps" / "web" / "dist"


def _register_org_ui(
    page: Any,
    base_url: str,
    *,
    email: str,
    full_name: str,
    org_name: str,
    password: str,
    page_errors: list[str],
) -> list[str]:
    failed_reqs: list[str] = []
    trace: list[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))
    page.on(
        "response",
        lambda r: (
            trace.append(f"RES {r.status} {r.request.method} {r.url}"),
            failed_reqs.append(f"{r.status} {r.url}") if r.status >= 400 else None,
        ),
    )
    page.on("requestfailed", lambda req: trace.append(f"FAIL {req.method} {req.url} {req.failure}"))
    page.on("framenavigated", lambda fr: trace.append(f"NAV {fr.url}"))
    page.on("console", lambda msg: trace.append(f"CONSOLE {msg.type}: {msg.text}"))
    page.goto(base_url, wait_until="networkidle")
    page.get_by_test_id("tab-register").click()
    page.get_by_test_id("input-email").fill(email)
    page.get_by_test_id("input-fullname").fill(full_name)
    page.get_by_test_id("input-orgname").fill(org_name)
    page.get_by_test_id("input-password").fill(password)
    page.get_by_test_id("btn-auth-submit").click()
    try:
        expect(page.get_by_test_id("user-role-badge")).to_contain_text(email, timeout=15000)
    except Exception as exc:
        ls_tok = page.evaluate("() => localStorage.getItem('ai_sdlc_access_token')")
        raise AssertionError(
            f"register failed for {email}! url={page.url!r} token_present={bool(ls_tok)} "
            f"page_errors={page_errors!r} trace={trace!r}"
        ) from exc
    return failed_reqs


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _pass_adapter() -> DeterministicMockAdapter:
    def _handler(messages: list[dict[str, str]], model_id: str) -> GatewayResponse:
        joined = "\n".join(m.get("content", "") for m in messages)
        if "MetaGPT ProductManager" in joined:
            payload: dict[str, Any] = {
                "clarification_questions": [],
                "target_files": ["src/app/main.py"],
                "expected_new_test_files": ["tests/test_subtract.py"],
            }
        elif "ChatDev" in joined and "Critique" in joined:
            payload = {"verdict": "accept", "issues": []}
        elif "ChatDev" in joined and "Review patch diff" in joined:
            payload = {"verdict": "APPROVE", "findings": [], "summary": "Looks clean"}
        elif "Coding Agent inside a sandboxed worktree" in joined:
            payload = {
                "test_files": {
                    "tests/test_subtract.py": (
                        "from app.main import subtract_numbers\n\n\n"
                        "def test_subtract_numbers() -> None:\n"
                        "    assert subtract_numbers(10, 3) == 7\n"
                    )
                },
                "code_files": {
                    "src/app/main.py": (
                        "from __future__ import annotations\n\n"
                        "from fastapi import FastAPI\n\n"
                        "app = FastAPI(title='Starter App')\n\n\n"
                        "@app.get('/health')\n"
                        "def health() -> dict[str, str]:\n"
                        "    return {'status': 'ok'}\n\n\n"
                        "def add_numbers(a: int, b: int) -> int:\n"
                        "    return a + b\n\n\n"
                        "def subtract_numbers(a: int, b: int) -> int:\n"
                        "    return a - b\n"
                    )
                },
            }
        else:
            payload = {"ok": True}
        return GatewayResponse(
            content=f"[CONTENT]{json.dumps(payload)}[/CONTENT]",
            actual_model_id=model_id,
            input_tokens=25,
            output_tokens=45,
            cached_tokens=0,
            usage_status="CONFIRMED",
        )

    return DeterministicMockAdapter(handler=_handler)


def _start_server(tmp_path: Path) -> tuple[str, uvicorn.Server, threading.Thread]:
    port = _free_port()
    settings = Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path / 'e2e.db'}",
        object_store_dir=str(tmp_path / "objects"),
        worktree_root=str(tmp_path / "worktrees"),
        jwt_secret="e2e-jwt-secret-key-min-32-chars!!",
        encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        github_webhook_secret="gh-sec",
        stripe_webhook_secret="st-sec",
        allow_local_git_repos=True,
    )
    app = create_app(settings, web_dist_dir=WEB_DIST_DIR)
    app.state.runtime.extra["override_adapters"] = {"default": _pass_adapter()}
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15.0
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=1.0) as resp:
                if resp.status == 200:
                    return base_url, server, thread
        except Exception:
            time.sleep(0.15)
    raise RuntimeError("Uvicorn E2E server failed to start cleanly.")


@pytest.mark.e2e
def test_browser_full_sdlc_flow_sse_rbac_and_tenant_isolation(tmp_path: Path) -> None:
    if not (WEB_DIST_DIR / "index.html").is_file():
        pytest.fail(f"Web UI bundle missing at {WEB_DIST_DIR}; run 'npm run build' in apps/web first.")

    base_url, server, thread = _start_server(tmp_path)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx_owner = browser.new_context()
            page = ctx_owner.new_page()
            page_errors: list[str] = []

            # 1. Register Org Alpha as Owner
            _register_org_ui(
                page,
                base_url,
                email="owner@alpha.dev",
                full_name="Alpha Owner",
                org_name="Alpha Corp",
                password="Password123!",
                page_errors=page_errors,
            )
            expect(page.get_by_test_id("user-role-badge")).to_contain_text("owner")

            # 2. Create Project + Scaffold starter template repository
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id("input-project-name").fill("Alpha Service")
            page.get_by_test_id("btn-create-project").click()
            expect(page.locator(".alert-ok")).to_contain_text("Alpha Service")

            page.get_by_test_id("input-repo-name").fill("alpha-api")
            page.get_by_test_id("btn-scaffold-repo").click()
            expect(page.locator(".alert-ok")).to_contain_text("alpha-api", timeout=30000)

            # 3. Queue task, stream SSE events, approve Spec -> Plan -> Delivery -> DELIVERED
            page.get_by_test_id("nav-tasks").click()
            page.locator("input[data-testid^='chk-repo-']").first.check()
            page.get_by_test_id("input-task-title").fill("Add subtract_numbers helper")
            page.get_by_test_id("input-task-req").fill(
                "Implement subtract_numbers in src/app/main.py returning a - b with unit tests."
            )
            page.get_by_test_id("btn-create-task").click()
            expect(page.get_by_test_id("task-detail-status")).to_be_visible()

            page.get_by_test_id("btn-drain-worker").click()
            expect(page.get_by_test_id("task-detail-status")).to_have_text("WAITING_APPROVAL", timeout=20000)

            # Approve spec
            expect(page.get_by_test_id("gate-panel")).to_contain_text("SPEC Gate")
            page.get_by_test_id("btn-approve-gate").click()
            expect(page.get_by_test_id("workbench-notice")).to_contain_text("spec")
            page.get_by_test_id("btn-drain-worker").click()
            expect(page.get_by_test_id("gate-panel")).to_contain_text("PLAN Gate", timeout=20000)

            # Approve plan
            page.get_by_test_id("btn-approve-gate").click()
            expect(page.get_by_test_id("workbench-notice")).to_contain_text("plan")
            page.get_by_test_id("btn-drain-worker").click()
            expect(page.get_by_test_id("gate-panel")).to_contain_text("DELIVERY Gate", timeout=90000)

            # Approve delivery
            page.get_by_test_id("btn-approve-gate").click()
            expect(page.get_by_test_id("workbench-notice")).to_contain_text("delivery")
            page.get_by_test_id("btn-drain-worker").click()
            expect(page.get_by_test_id("task-detail-status")).to_have_text("DELIVERED", timeout=30000)

            # Verify SSE timeline captured real task events
            sse_text = page.get_by_test_id("sse-timeline").inner_text()
            assert "task.created" in sse_text
            assert "task.finished" in sse_text

            # Launch private preview in UI
            page.get_by_test_id("btn-launch-preview").click()
            expect(page.get_by_test_id("workbench-error")).not_to_be_visible()
            expect(page.get_by_test_id("preview-panel")).to_contain_text(
                "Preview prv_", timeout=15000
            )

            # Check remaining control-plane navigation tabs render cleanly
            page.get_by_test_id("nav-models").click()
            expect(page.get_by_text("Store BYOK Credential")).to_be_visible()
            page.get_by_test_id("nav-billing").click()
            expect(page.get_by_text("Usage Ledger")).to_be_visible()

            # 4. Invite developer in Governance tab -> test Developer RBAC denial on release promotion
            page.get_by_test_id("nav-governance").click()
            page.get_by_placeholder("teammate@acme.dev").fill("dev@alpha.dev")
            page.get_by_role("button", name="Invite").click()
            expect(page.get_by_text("Invite token:")).to_be_visible()
            inv_banner = page.get_by_text("Invite token:").inner_text()
            invite_token = inv_banner.split("Invite token:", 1)[1].strip()

            ctx_dev = browser.new_context()
            dev_page = ctx_dev.new_page()
            failed_reqs = _register_org_ui(
                dev_page,
                base_url,
                email="dev@alpha.dev",
                full_name="Alpha Dev",
                org_name="Dev Sandbox",
                password="Password123!",
                page_errors=page_errors,
            )
            assert "401" not in str(failed_reqs), failed_reqs

            dev_page.get_by_test_id("nav-governance").click()
            dev_page.get_by_placeholder("Paste invite token to join another org...").fill(invite_token)
            dev_page.get_by_role("button", name="Accept").click()
            expect(dev_page.locator(".alert-ok")).to_contain_text("Invitation accepted")
            dev_page.reload(wait_until="networkidle")

            # Switch active org to Alpha Corp (where user has developer role)
            alpha_opt = dev_page.locator("[data-testid='select-active-org'] option", has_text="Alpha Corp").get_attribute(
                "value"
            )
            assert alpha_opt
            dev_page.get_by_test_id("select-active-org").select_option(alpha_opt)
            expect(dev_page.get_by_test_id("user-role-badge")).to_contain_text("developer")

            # Developer tries to promote release -> visible RBAC 403 error
            dev_page.get_by_test_id("nav-tasks").click()
            dev_page.get_by_test_id("btn-promote-release").click(timeout=15000)
            expect(dev_page.get_by_test_id("workbench-error")).to_contain_text("release:approve")

            # 5. Cross-tenant isolation check from Tenant Beta browser session
            page.get_by_test_id("nav-tasks").click()
            alpha_task_url = page.locator("[data-testid^='task-item-']").first.get_attribute("data-testid")
            assert alpha_task_url
            alpha_task_id = alpha_task_url.replace("task-item-", "")

            ctx_beta = browser.new_context()
            beta_page = ctx_beta.new_page()
            _register_org_ui(
                beta_page,
                base_url,
                email="intruder@beta.dev",
                full_name="Beta User",
                org_name="Beta Corp",
                password="Password123!",
                page_errors=page_errors,
            )
            expect(beta_page.get_by_text("No tasks queued yet.")).to_be_visible()

            leak_status = beta_page.evaluate(
                """async (tid) => {
                    const tok = localStorage.getItem('ai_sdlc_access_token');
                    const r = await fetch('/api/v1/tasks/' + tid, { headers: { Authorization: 'Bearer ' + tok } });
                    return r.status;
                }""",
                alpha_task_id,
            )
            assert leak_status in (403, 404)

            assert not page_errors, f"Uncaught JS page errors: {page_errors}"
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
