"""Multi-tenant concurrent load test against FastAPI + SQLite/worker measuring p50/p95/max latency & tenant isolation."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from ai_sdlc_saas.api.app import create_app
from ai_sdlc_saas.config import Settings
from ai_sdlc_saas.gateway.adapters import DeterministicMockAdapter, GatewayResponse


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round((pct / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def run_load_test(num_tenants: int = 10, requests_per_tenant: int = 20) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="sdlc_load_") as tmp:
        tmp_path = Path(tmp)
        port = _free_port()
        settings = Settings(
            env="test",
            database_url=f"sqlite:///{tmp_path / 'load.db'}",
            object_store_dir=str(tmp_path / "objects"),
            worktree_root=str(tmp_path / "worktrees"),
            jwt_secret="load-test-jwt-secret-key-min-32chars!",
            encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
            rate_limit_rpm=5000,
            allow_local_git_repos=True,
        )
        app = create_app(settings)
        app.state.runtime.extra["override_adapters"] = {
            "default": DeterministicMockAdapter(
                handler=lambda _m, model_id: GatewayResponse(
                    content="[CONTENT]{\"ok\": true}[/CONTENT]",
                    actual_model_id=model_id,
                    input_tokens=10,
                    output_tokens=20,
                    cached_tokens=0,
                    usage_status="CONFIRMED",
                )
            )
        }
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        th = threading.Thread(target=server.run, daemon=True)
        th.start()
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.time() + 15.0
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{base_url}/healthz", timeout=1.0) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.1)

        register_latencies_ms: list[float] = []
        api_latencies_ms: list[float] = []
        created_task_ids: dict[int, list[str]] = {}
        tokens: dict[int, str] = {}
        errors: list[str] = []
        lock = threading.Lock()

        def _tenant_worker(tid: int) -> None:
            with httpx.Client(base_url=base_url, timeout=15.0) as client:
                t0 = time.perf_counter()
                reg = client.post(
                    "/api/v1/auth/register",
                    json={
                        "email": f"load{tid}@tenant{tid}.dev",
                        "password": "Password123!",
                        "full_name": f"Load User {tid}",
                        "org_name": f"Load Corp {tid}",
                    },
                )
                reg_lat = (time.perf_counter() - t0) * 1000.0
                if reg.status_code != 201:
                    with lock:
                        errors.append(f"t{tid} register={reg.status_code}: {reg.text}")
                    return
                tok = reg.json()["access_token"]
                hdr = {"Authorization": f"Bearer {tok}"}
                tp = time.perf_counter()
                proj = client.post("/api/v1/projects", headers=hdr, json={"name": f"Project {tid}"})
                proj_lat = (time.perf_counter() - tp) * 1000.0
                if proj.status_code != 201:
                    with lock:
                        errors.append(f"t{tid} project={proj.status_code}")
                    return
                pid = proj.json()["id"]
                tids_local: list[str] = []
                local_api_lats = [proj_lat]

                # Steady-state authenticated tenant API workload
                for req_idx in range(requests_per_tenant):
                    ts = time.perf_counter()
                    if req_idx < 2:
                        resp = client.post(
                            "/api/v1/tasks",
                            headers=hdr,
                            json={
                                "project_id": pid,
                                "title": f"Load docs {tid}-{req_idx}",
                                "requirement_text": "Update README compliance notes for load test verification.",
                                "task_type": "docs",
                                "repo_ids": [],
                            },
                        )
                        if resp.status_code != 201:
                            with lock:
                                errors.append(f"t{tid} create_task={resp.status_code}")
                        else:
                            tids_local.append(resp.json()["id"])
                    elif req_idx % 2 == 0:
                        resp = client.get("/api/v1/tasks", headers=hdr)
                        if resp.status_code != 200:
                            with lock:
                                errors.append(f"t{tid} list_tasks={resp.status_code}")
                        else:
                            returned = {row["id"] for row in resp.json()}
                            if not returned.issubset(set(tids_local)):
                                with lock:
                                    errors.append(f"t{tid} saw foreign tasks: {returned - set(tids_local)}")
                    else:
                        resp = client.get("/api/v1/projects", headers=hdr)
                        if resp.status_code != 200:
                            with lock:
                                errors.append(f"t{tid} list_projects={resp.status_code}")
                    local_api_lats.append((time.perf_counter() - ts) * 1000.0)

                with lock:
                    register_latencies_ms.append(reg_lat)
                    api_latencies_ms.extend(local_api_lats)
                    created_task_ids[tid] = tids_local
                    tokens[tid] = tok

        t_start = time.perf_counter()
        try:
            with ThreadPoolExecutor(max_workers=num_tenants) as pool:
                list(pool.map(_tenant_worker, range(num_tenants)))

            # Cross-tenant negative probe across every pair (tenant i cannot read tenant (i+1)'s task)
            cross_tenant_leaks = 0
            with httpx.Client(base_url=base_url, timeout=10.0) as client:
                for tid in range(num_tenants):
                    other_tid = (tid + 1) % num_tenants
                    other_tasks = created_task_ids.get(other_tid, [])
                    if not other_tasks or tid not in tokens:
                        continue
                    probe = client.get(
                        f"/api/v1/tasks/{other_tasks[0]}",
                        headers={"Authorization": f"Bearer {tokens[tid]}"},
                    )
                    if probe.status_code not in (403, 404):
                        cross_tenant_leaks += 1
        finally:
            server.should_exit = True
            th.join(timeout=5.0)
            app.state.runtime.engine.dispose()

        total_elapsed = time.perf_counter() - t_start
        sorted_api = sorted(api_latencies_ms)
        sorted_reg = sorted(register_latencies_ms)
        api_p50 = round(_percentile(sorted_api, 50.0), 2)
        api_p95 = round(_percentile(sorted_api, 95.0), 2)
        api_max = round(max(sorted_api) if sorted_api else 0.0, 2)
        reg_p95 = round(_percentile(sorted_reg, 95.0), 2)
        return {
            "ok": not errors and cross_tenant_leaks == 0 and api_p95 < 500.0,
            "concurrent_tenants": num_tenants,
            "api_requests_measured": len(sorted_api),
            "register_burst_measured": len(sorted_reg),
            "total_elapsed_s": round(total_elapsed, 3),
            "throughput_rps": round((len(sorted_api) + len(sorted_reg)) / max(0.001, total_elapsed), 2),
            "api_p50_ms": api_p50,
            "api_p95_ms": api_p95,
            "api_max_ms": api_max,
            "register_burst_p95_ms": reg_p95,
            "cross_tenant_leaks": cross_tenant_leaks,
            "errors": errors,
        }


def main() -> int:
    result = run_load_test()
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
