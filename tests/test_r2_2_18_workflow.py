from __future__ import annotations

import pytest

from generation.workflow import (
    WORKFLOW_STATES,
    begin_image_generation,
    confirm_article,
    confirm_final_draft,
    confirm_images,
    finish_image_generation,
    initialize_workflow,
    prepare_fusion,
    require_export_ready,
)
from generation.image_budget import calculate_image_budget, image_cost_preview, normalize_image_plan
from providers.text_provider import ProviderError


def _state() -> dict:
    return {
        "task_id": "workflow-test",
        "status": "completed",
        "article": {"title": "标题", "sections": [{"heading": "一", "body": "正文"}]},
        "cover": {"role": "cover", "path": "images/cover.png", "status": "completed"},
        "inline_images": [],
    }


def test_workflow_requires_each_customer_gate():
    state = _state()
    initialize_workflow(state)
    assert state["workflow_state"] == "article_pending_confirmation"
    with pytest.raises(ProviderError) as error:
        begin_image_generation(state)
    assert error.value.code == "WORKFLOW_NOT_READY"

    confirm_article(state)
    assert state["workflow_state"] == "article_confirmed"
    begin_image_generation(state)
    assert state["workflow_state"] == "images_generating"
    finish_image_generation(state)
    assert state["workflow_state"] == "images_pending_confirmation"
    confirm_images(state)
    assert state["workflow_state"] == "fusion_pending"
    prepare_fusion(state)
    assert state["fusion_status"]["model_calls"] == 0
    confirm_final_draft(state)
    require_export_ready(state)
    assert state["workflow_state"] == "final_draft_confirmed"


def test_workflow_states_are_stable_and_exports_cannot_skip_preview():
    assert WORKFLOW_STATES == (
        "article_draft",
        "article_pending_confirmation",
        "article_confirmed",
        "images_pending_generation",
        "images_generating",
        "images_pending_confirmation",
        "fusion_pending",
        "final_draft_pending_preview",
        "final_draft_confirmed",
        "export_ready",
        "exported",
    )
    state = _state()
    initialize_workflow(state)
    with pytest.raises(ProviderError) as error:
        require_export_ready(state)
    assert error.value.code == "FINAL_DRAFT_NOT_READY"


def test_low_cost_plan_is_two_images_with_explicit_estimate():
    assert normalize_image_plan("low") == "low"
    assert calculate_image_budget(1, "low") == 2
    preview = image_cost_preview(1, 1200, "low", unit_price=0.10)
    assert preview["image_calls"] == 2
    assert preview["estimated_cost"] == 0.2
