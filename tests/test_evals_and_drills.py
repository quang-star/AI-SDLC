"""P14 verification tests: eval dev/holdout benchmark, multi-tenant load smoke, backup drill & secret scan."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load_script_module(name: str, rel_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_p14_eval_suite_load_drill_and_secret_scan(tmp_path: Path) -> None:
    # 1. Evaluation benchmark (dev + holdout splits, zero leakage)
    run_evals = _load_script_module("run_evals_mod", "scripts/run_evals.py")
    eval_report = run_evals.run_eval_suite()
    assert eval_report["ok"] is True, eval_report
    assert eval_report["splits"]["dev"]["accuracy"] == 1.0
    assert eval_report["splits"]["holdout"]["accuracy"] == 1.0

    # 2. Multi-tenant concurrent load test (p95 SLA < 500ms, zero cross-tenant leakage)
    load_test = _load_script_module("load_test_mod", "scripts/load_test.py")
    load_res = load_test.run_load_test(num_tenants=10, requests_per_tenant=20)
    assert load_res["ok"] is True, load_res
    assert load_res["cross_tenant_leaks"] == 0
    assert load_res["api_p95_ms"] < 500.0
    assert not load_res["errors"]

    # 3. Backup & restore drill
    backup_mod = _load_script_module("backup_drill_mod", "scripts/backup_restore_drill.py")
    db_path, obj_dir = backup_mod._bootstrap_synthetic_source(tmp_path / "src")
    drill_res = backup_mod.run_backup_restore_drill(
        source_sqlite_path=db_path,
        source_object_store_dir=obj_dir,
        work_dir=tmp_path / "work",
    )
    assert drill_res["ok"] is True
    assert drill_res["integrity_check"] == "ok"

    # 4. Repository static secret hygiene scan
    secrets_mod = _load_script_module("check_secrets_mod", "scripts/check_secrets.py")
    assert secrets_mod.scan_directory(ROOT) == []
