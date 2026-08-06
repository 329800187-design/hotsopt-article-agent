from pathlib import Path

from generation.image_budget import calculate_image_budget, image_plan_for, normalize_image_plan


def test_three_to_five_image_modes_have_exact_initial_budget():
    expected = {"three": 3, "four": 4, "five": 5}
    for mode, calls in expected.items():
        plan = image_plan_for(1200, mode)
        assert plan["cover"] == 1
        assert plan["inline_count"] == calls - 1
        assert plan["max_calls"] == calls
        assert calculate_image_budget(2, mode) == calls * 2


def test_image_mode_aliases_are_stable():
    assert normalize_image_plan("3") == "three"
    assert normalize_image_plan("4") == "four"
    assert normalize_image_plan("5") == "five"


def test_hotspot_ui_combines_keyword_and_source_filters():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert 'key="rc1_hotspot_source"' in source
    assert '"summary", "category", "source_name", "source"' in source
    assert 'key="rc1_hotspot_sort"' in source


def test_customer_input_limits_and_image_endpoint_are_expanded():
    ui_source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    api_source = Path("api.py").read_text(encoding="utf-8")
    assert "最多 20 个链接" in ui_source
    assert "unique_lines[:20]" in ui_source
    assert 'Literal["none", "economy", "standard", "three", "four", "five"]' in api_source
    assert 'Field(default=0, ge=0, le=4)' in api_source
