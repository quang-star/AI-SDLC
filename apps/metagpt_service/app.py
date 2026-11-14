"""Standalone MetaGPT BA + Architect FastAPI sidecar service."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from ai_sdlc.contracts import RepoContext
from ai_sdlc_saas.adapters.metagpt_service import analyze_requirement_gaps, extract_content_block

app = FastAPI(title="AI SDLC MetaGPT BA Service", version="0.1.0")


class AnalyzeSpecRequest(BaseModel):
    requirement_text: str
    repo_context: dict[str, Any]
    raw_llm_output: str | None = None


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "metagpt-ba"}


@app.post("/analyze")
def analyze(body: AnalyzeSpecRequest) -> dict[str, Any]:
    ctx = RepoContext.from_dict(body.repo_context)
    questions = analyze_requirement_gaps(body.requirement_text, ctx)
    parsed = extract_content_block(body.raw_llm_output) if body.raw_llm_output else None
    return {"clarification_questions": questions, "parsed_draft": parsed}
