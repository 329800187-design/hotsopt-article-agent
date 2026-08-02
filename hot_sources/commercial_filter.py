from __future__ import annotations

import re
import unicodedata
from typing import Any

from modules.models import HotTopic


ALLOWED_HOTSPOT_CLASSES = {
    "NEWS",
    "SOCIAL_EVENT",
    "TECH",
    "FINANCE",
    "ENTERTAINMENT",
    "SPORTS",
}

BLOCKED_HOTSPOT_CLASSES = {
    "COMMERCIAL_PROMOTION",
    "ECOMMERCE_PRODUCT",
    "RECRUITMENT_PROMOTION",
    "INVALID",
}

PROMOTION_TERMS = {
    "\u5238\u540e": "coupon_price",
    "\u539f\u4ef7": "original_price",
    "\u5230\u624b\u4ef7": "final_price",
    "\u552e\u4ef7": "sale_price",
    "\u4f18\u60e0\u4ef7": "discount_price",
    "\u9650\u65f6\u4ef7": "limited_price",
    "\u6708\u9500": "monthly_sales",
    "\u9500\u91cf": "sales_volume",
    "\u5df2\u552e": "sold_count",
    "\u9886\u5238": "claim_coupon",
    "\u4f18\u60e0\u5238": "coupon",
    "\u4e0b\u5355": "purchase_action",
    "\u8d2d\u4e70": "purchase_action",
    "\u5305\u90ae": "free_shipping",
    "\u65d7\u8230\u5e97": "store",
    "\u76f4\u64ad\u95f4": "live_sales",
    "\u540c\u6b3e": "same_product",
    "\u5546\u54c1\u89c4\u683c": "product_spec",
    "\u7acb\u5373\u62a2\u8d2d": "buy_now",
}

PRODUCT_TERMS = {
    "\u773c\u819c": "product_name",
    "\u7ae5\u88c5": "product_name",
    "\u98ce\u6247": "product_name",
    "\u9762\u85d5": "product_name",
    "\u7d20\u80a5\u80a0": "product_name",
    "\u7259\u818f": "product_name",
    "\u9762\u819c": "product_name",
    "\u6c14\u6ce1\u6c34": "product_name",
    "\u5b89\u7761\u88e4": "product_name",
}

RECRUITMENT_TERMS = {
    "\u62db\u8058": "recruitment",
    "\u6025\u62db": "recruitment",
    "\u5c97\u4f4d": "recruitment",
    "\u6708\u85aa": "salary",
    "\u7b80\u5386": "resume",
}

BRAND_PRODUCT_PRICE_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9-]{2,}|[\u4e00-\u9fff]{2,12}).{0,18}(?:[¥￥]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*\u5143)"
)
PRICE_RE = re.compile(r"(?:[¥￥]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*\u5143)")
SALES_RE = re.compile(r"(?:\u6708\u9500|\u70ed\u9500|\u5df2\u552e)\s*\d+")


def _text_for_topic(topic: HotTopic | dict[str, Any]) -> str:
    if isinstance(topic, HotTopic):
        values = [topic.title, topic.summary, topic.hot_value or "", topic.source_name, topic.category]
    else:
        values = [str(topic.get(key) or "") for key in ("title", "summary", "hot_value", "source_name", "category")]
    return unicodedata.normalize("NFKC", " ".join(values))


def classify_commercial_topic(topic: HotTopic | dict[str, Any]) -> dict[str, Any]:
    text = _text_for_topic(topic)
    signals: list[str] = []
    for term, name in {**PROMOTION_TERMS, **PRODUCT_TERMS, **RECRUITMENT_TERMS}.items():
        if term in text:
            signals.append(name if name not in signals else term)
    if PRICE_RE.search(text):
        signals.append("price")
    if SALES_RE.search(text):
        signals.append("sales_count")
    if BRAND_PRODUCT_PRICE_RE.search(text):
        signals.append("brand_product_price")
    product_signal_count = sum(item in signals for item in {"product_name", "brand_product_price"})
    promotion_signal_count = sum(item in signals for item in {
        "coupon_price",
        "original_price",
        "final_price",
        "sale_price",
        "discount_price",
        "limited_price",
        "monthly_sales",
        "sales_volume",
        "sold_count",
        "claim_coupon",
        "coupon",
        "purchase_action",
        "free_shipping",
        "store",
        "live_sales",
        "buy_now",
        "price",
        "sales_count",
    })
    recruitment_signal_count = sum(item in signals for item in {"recruitment", "salary", "resume"})
    score = promotion_signal_count + product_signal_count * 2 + recruitment_signal_count * 2
    reason = "NEWS"
    if recruitment_signal_count >= 2:
        reason = "RECRUITMENT_PROMOTION"
    elif promotion_signal_count >= 2 and (product_signal_count or "sales_count" in signals or "brand_product_price" in signals):
        reason = "ECOMMERCE_PRODUCT"
    elif promotion_signal_count >= 3:
        reason = "COMMERCIAL_PROMOTION"
    elif not str(text).strip():
        reason = "INVALID"
    return {
        "hotspot_class": reason,
        "commercial_score": int(score),
        "matched_signals": list(dict.fromkeys(signals)),
        "filter_reason": reason if reason in BLOCKED_HOTSPOT_CLASSES else "",
        "is_blocked": reason in BLOCKED_HOTSPOT_CLASSES,
    }


def attach_commercial_classification(topic: HotTopic) -> HotTopic:
    result = classify_commercial_topic(topic)
    topic.raw_data = {**(topic.raw_data or {}), "commercial_filter": result}
    return topic


def filter_public_hotspots(topics: list[HotTopic]) -> tuple[list[HotTopic], list[dict[str, Any]]]:
    valid: list[HotTopic] = []
    filtered: list[dict[str, Any]] = []
    for topic in topics:
        attach_commercial_classification(topic)
        result = dict((topic.raw_data or {}).get("commercial_filter") or {})
        if result.get("is_blocked"):
            filtered.append({
                "hotspot_id": topic.id,
                "title": topic.title,
                "commercial_score": result.get("commercial_score", 0),
                "matched_signals": result.get("matched_signals", []),
                "filter_reason": result.get("filter_reason") or result.get("hotspot_class"),
            })
            continue
        valid.append(topic)
    return valid, filtered


def blocked_topic_reason(topic: HotTopic | dict[str, Any]) -> dict[str, Any] | None:
    result = classify_commercial_topic(topic)
    return result if result.get("is_blocked") else None
