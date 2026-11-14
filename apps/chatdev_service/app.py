"""Standalone ChatDev 2.0 YAML QA/Critique/Review FastAPI sidecar service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from ai_sdlc_saas.adapters.chatdev_service import load_and_validate_workflow

_WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / "workflows"

app = FastAPI(title="AI SDLC ChatDev 2.0 QA & Review Service", version="0.1.0")


class ValidateWorkflowRequest(BaseModel):
    workflow_name: str


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "chatdev-qa-review"}


@app.post("/validate-workflow")
def validate_workflow(body: ValidateWorkflowRequest) -> dict[str, Any]:
    target = (_WORKFLOWS_DIR / f"{body.workflow_name}.yaml").resolve()
    data = load_and_validate_workflow(target)
    return {"valid": True, "workflow": data}
