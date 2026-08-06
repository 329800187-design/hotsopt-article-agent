from __future__ import annotations

import re
from typing import Any


# ── 热点分类体系 ──
CATEGORY_RULES: dict[str, dict[str, Any]] = {
    "A": {
        "name": "快讯/娱乐/奇闻",
        "keywords": [
            "明星", "网红", "爆料", "反转", "热议", "网友", "离谱",
            "曝光", "争议", "刷屏", "调侃", "翻车", "内幕", "八卦",
            "综艺", "直播", "段子", "搞笑", "吐槽", "聊天记录",
        ],
        "target_chars": 1200,
        "structure": [
            ("钩子开头", "一句话抓住读者，制造悬念或冲击感"),
            ("30秒速览", "3个要点快速交代发生了什么"),
            ("单点深挖", "只讲一个核心看点，不做全景式展开"),
            ("观点判断", "给出明确立场或态度，不骑墙"),
            ("结尾互动", "向读者提问或邀请评论"),
        ],
    },
    "B": {
        "name": "社会/民生/案件",
        "keywords": [
            "女子", "男子", "老人", "孩子", "学生", "医院", "学校",
            "死亡", "坠楼", "车祸", "火灾", "诈骗", "维权", "判决",
            "事故", "失踪", "跳楼", "自杀", "打人", "殴打", "猥亵",
            "强拆", "食品", "疫苗", "药品", "幼儿园", "小学", "中学",
        ],
        "target_chars": 1500,
        "structure": [
            ("细节画面", "还原事件现场细节，让读者身临其境"),
            ("事件还原", "按时间线梳理事件经过"),
            ("追问反思", "提出3个以上的深层问题"),
            ("同类参照", "引入过去类似案例作为参照系"),
            ("普通人启示", "从事件中总结对普通人有什么启发"),
        ],
    },
    "C": {
        "name": "国际/军事/政策/灾害",
        "keywords": [
            "日本", "美国", "俄罗斯", "乌克兰", "欧盟", "北约",
            "政策", "制裁", "核", "导弹", "地震", "强震", "海啸",
            "火山", "台风", "洪水", "飓风", "经济", "GDP", "贸易战",
            "军演", "冲突", "协议", "条约", "联合国",
        ],
        "target_chars": 1600,
        "structure": [
            ("利益导语", "直接点明这件事为什么和你有关"),
            ("事件全貌", "全景式梳理事件经过和关键节点"),
            ("背景解释", "用通俗语言解释专业概念和背景"),
            ("三层分析", "短期影响 + 中期趋势 + 长期格局"),
            ("影响判断", "具体说清谁受益、谁受损"),
            ("后续关注", "给出可追踪的指标和时间节点"),
        ],
    },
}

# 默认走 A 类
DEFAULT_CATEGORY = "A"

_KEYWORD_WEIGHTS: dict[str, dict[str, int]] = {}
for _cat, _rules in CATEGORY_RULES.items():
    _KEYWORD_WEIGHTS[_cat] = {kw: len(kw) for kw in _rules["keywords"]}


def classify_topic(title: str, category_label: str = "", summary: str = "") -> dict[str, Any]:
    """根据标题、分类标签、摘要自动判定话题类别。
    
    Returns: {
        "category_key": "A"|"B"|"C",
        "category_name": "...",
        "target_chars": 1200|1500|1600,
        "structure": [...],
        "classification_reason": "...",
    }
    """
    title = str(title or "").strip()
    summary = str(summary or "").strip()
    catalog_label = str(category_label or "").strip()
    
    combined = f"{title} {summary} {catalog_label}"
    
    # 统计各分类关键词命中（按长度加权）
    scores: dict[str, float] = {}
    for cat, weights in _KEYWORD_WEIGHTS.items():
        score = 0.0
        for kw, weight in weights.items():
            if kw in combined:
                score += weight
        scores[cat] = score
    
    # 选最高分，平局时优先B类（社会民生最需要足够篇幅）
    max_score = max(scores.values()) if scores else 0
    if max_score == 0:
        chosen = DEFAULT_CATEGORY
        reason = "未命中任何分类关键词，默认归入快讯/娱乐/奇闻"
    else:
        # 找最高分类，平局优先B
        candidates = [cat for cat, sc in scores.items() if sc == max_score]
        if "B" in candidates and len(candidates) > 1:
            chosen = "B"
        else:
            chosen = candidates[0]
        reason = f'命中分类关键词（{CATEGORY_RULES[chosen]["name"]}）：得分 {max_score:.0f}'
    
    rules = CATEGORY_RULES[chosen]
    return {
        "category_key": chosen,
        "category_name": rules["name"],
        "target_chars": rules["target_chars"],
        "structure": rules["structure"],
        "classification_reason": reason,
    }
