"""Unified StageExecutors factory composing P6 MetaGPT + P7 ChatDev + P8 Coding + P9 Verifier + P10 Publisher."""

from __future__ import annotations

from ai_sdlc_saas.adapters.chatdev_service import chatdev_code_review_stage, chatdev_qa_critique_stage
from ai_sdlc_saas.adapters.coding_runner import (
    coding_stage,
    execution_plan_stage,
    independent_verify_stage,
)
from ai_sdlc_saas.adapters.metagpt_service import metagpt_architect_stage, metagpt_ba_stage
from ai_sdlc_saas.delivery.publisher import publish_pr_stage
from ai_sdlc_saas.orchestrator.graph import StageExecutors


def build_production_executors() -> StageExecutors:
    """Wire all real production stage implementations (P6–P10) into the 16-stage LangGraph."""
    return StageExecutors(
        ba_specify=metagpt_ba_stage,
        ba_refine=metagpt_ba_stage,
        architect_design=metagpt_architect_stage,
        qa_critique=chatdev_qa_critique_stage,
        execution_plan=execution_plan_stage,
        code_and_tdd=coding_stage,
        independent_verify=independent_verify_stage,
        code_review=chatdev_code_review_stage,
        publish_pr=publish_pr_stage,
    )
