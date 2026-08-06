from __future__ import annotations

from typing import Any, Iterable


ANGLE_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "angle_id": "news",
        "angle_name": "新闻资讯",
        "name": "新闻资讯",
        "core_question": "发生了什么，读者现在需要知道哪些已确认信息？",
        "opening_strategy": "从已确认的事件事实和时间线切入",
        "structure": ["事件概述", "关键信息", "背景资料", "影响范围", "后续关注"],
        "instruction": "事实优先，清晰简洁，少做主观判断，适合资讯账号。",
        "must_avoid": ["未经来源支持的细节", "把推测写成事实"],
        "visual_direction": "信息图式新闻场景、清晰主体、客观情绪",
    },
    {
        "angle_id": "social_observation",
        "angle_name": "社会观察",
        "name": "社会观察",
        "core_question": "这件事影响了哪些普通人，折射出什么社会背景？",
        "opening_strategy": "从受影响群体的真实生活切入",
        "structure": ["事件现象", "受影响群体", "社会背景", "深层原因", "可能变化"],
        "instruction": "关注普通人的生活影响，区分事实、观察和分析，避免煽动。",
        "must_avoid": ["标签化群体", "无证据的因果判断"],
        "visual_direction": "城市生活、人群影响、社会空间和克制情绪",
    },
    {
        "angle_id": "emotional",
        "angle_name": "情绪共鸣",
        "name": "情绪共鸣",
        "core_question": "事件中哪些人物处境和生活情绪值得被看见？",
        "opening_strategy": "从具体人物或生活场景切入",
        "structure": ["生活场景", "人物处境", "情绪冲突", "共鸣分析", "克制收束"],
        "instruction": "语言自然、有代入感，但不得虚构人物、对白或重大事实。",
        "must_avoid": ["虚构故事冒充事实", "过度煽情"],
        "visual_direction": "人物近景、生活场景、温和但有张力的情绪",
    },
    {
        "angle_id": "commentary",
        "angle_name": "观点评论",
        "name": "观点评论",
        "core_question": "争议核心是什么，事实与观点应如何区分？",
        "opening_strategy": "从核心争议和公共问题切入",
        "structure": ["核心争议", "支持观点", "反方观点", "作者判断", "建议与结论"],
        "instruction": "观点明确，事实与判断分开，引用可核验信息，不使用攻击性极端表达。",
        "must_avoid": ["人身攻击", "极端化结论", "把评论写成官方结论"],
        "visual_direction": "象征性冲突、对比构图、理性编辑部视觉",
    },
    {
        "angle_id": "story",
        "angle_name": "故事化解读",
        "name": "故事化解读",
        "core_question": "如何用清晰叙事帮助读者理解事件的来龙去脉？",
        "opening_strategy": "从人物、场景或关键节点展开叙事",
        "structure": ["人物或场景开篇", "问题出现", "冲突推进", "事实补充", "意义总结"],
        "instruction": "用叙事帮助理解，但所有真实事件信息必须来自话题快照和来源。",
        "must_avoid": ["虚构重大事实", "把情景化表达伪装成采访"],
        "visual_direction": "电影感场景、人物行动、连贯叙事画面",
    },
)

DEFAULT_ANGLE_IDS = ("news", "commentary", "social_observation", "emotional", "story")


def available_angles() -> list[dict[str, Any]]:
    return [dict(item) for item in ANGLE_CATALOG]


def plan_angles(count: int, selected_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    count = max(1, min(5, int(count)))
    by_id = {item["angle_id"]: item for item in ANGLE_CATALOG}
    if selected_ids is None:
        angle_ids = list(DEFAULT_ANGLE_IDS[:count])
    else:
        angle_ids = [str(value) for value in selected_ids]
        if len(angle_ids) != count or len(set(angle_ids)) != len(angle_ids):
            raise ValueError("角度数量必须与文章数量一致，且不能重复")
        if any(value not in by_id for value in angle_ids):
            raise ValueError("存在不支持的文章角度")
    return [dict(by_id[angle_id]) for angle_id in angle_ids]


def get_angle(angle_id: str) -> dict[str, Any]:
    for item in ANGLE_CATALOG:
        if item["angle_id"] == angle_id:
            return dict(item)
    raise ValueError("存在不支持的文章角度")
