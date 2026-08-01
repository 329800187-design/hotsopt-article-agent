from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote_plus, urlparse, urlunparse

from modules.app_paths import research_root
from modules.network import create_http_client


TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "spm", "from"}
BOILERPLATE_RE = re.compile(r"登录|注册|隐私政策|版权声明|联系我们|返回顶部|推荐阅读|热门推荐|广告|用户协议|关注我们")
GENERIC_TOPIC_TERMS = {"热点", "消息", "事件", "相关", "最新", "今天", "昨日", "明日", "目前", "表示", "发布", "报道", "情况", "回应", "测试", "原文", "同一", "正式"}
ORG_SUFFIXES = ("公司", "集团", "政府", "市政府", "委员会", "学校", "医院", "协会", "大学", "部门", "会议", "法院")
PERSON_SUFFIXES = ("先生", "女士", "市长", "部长", "局长", "总统", "议员")
OFFICIAL_DOMAIN_WHITELIST = {"mfa.gov.cn", "gov.cn", "gov", "gov.tw", "gov.hk"}
PAGE_NOISE_RE = re.compile(r"相关推荐|热门推荐|版权声明|作者声明|导航菜单|评论区|其他视频标题|页面底部新闻列表|推荐阅读|猜你喜欢|广告")
SOURCE_TEMPLATE_NOISE_RE = re.compile(
    r"\{\{[^{}]{0,200}\}\}|"
    r"\b(?:dynamicData|subjectData|item\.reporter_name|item\.tag|reporter_name)\b|"
    r"未发布文章|文章未发布|仅支持\s*15\s*分钟预览|后台刷新重置预览|"
    r"打开[\u4e00-\u9fffA-Za-z0-9]{0,12}新闻|阅读体验更佳|更多内容请打开|"
    r"打开客户端|下载客户端|下载\s*APP|APP内打开|广告|相关推荐|推荐阅读|热门推荐|猜你喜欢|"
    r"导航|返回首页|版权声明|用户协议|隐私政策|登录|注册"
)
FACT_ACTION_MARKERS = (
    " announced ",
    " said ",
    " reported ",
    " confirmed ",
    " released ",
    "表示",
    "称",
    "发布",
    "公布",
    "通报",
    "确认",
    "回应",
    "宣布",
    "披露",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_url(url: str) -> str:
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return str(url).strip()
    query = "&".join(
        f"{key}={value[0]}"
        for key, value in sorted(parse_qs(parsed.query).items())
        if key.lower() not in TRACKING_KEYS and value
    )
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def _normalize_text(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "")).lower()
    if text:
        return text
    fallback = re.sub(r"[\W_]+", "", str(value or ""), flags=re.UNICODE).lower()
    return fallback or re.sub(r"\s+", "", str(value or "")).lower()


def _content_fingerprint(value: str) -> str:
    return hashlib.sha256(_normalize_text(value)[:12000].encode("utf-8")).hexdigest()


def clean_source_text(text: str) -> dict[str, Any]:
    """Remove page/template noise before source text reaches fact cards or prompts."""
    original = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    original_paragraphs = [item.strip() for item in re.split(r"\n+|(?<=[。！？；])\s+", original) if item.strip()]
    removed_noise_count = 0
    duplicate_block_count = 0
    cleaned_paragraphs: list[str] = []
    seen: set[str] = set()
    for raw in original_paragraphs:
        paragraph = re.sub(r"\s+", " ", raw).strip()
        if not paragraph:
            continue
        before = paragraph
        paragraph, variable_hits = re.subn(r"\{\{[^{}]{0,200}\}\}", "", paragraph)
        removed_noise_count += variable_hits
        if SOURCE_TEMPLATE_NOISE_RE.search(before) or PAGE_NOISE_RE.search(before) or BOILERPLATE_RE.search(before):
            removed_noise_count += 1
            continue
        paragraph = paragraph.strip(" ，,:：")
        normalized = _normalize_text(paragraph)
        if len(paragraph) < 12 or not normalized:
            continue
        if normalized in seen:
            duplicate_block_count += 1
            continue
        seen.add(normalized)
        cleaned_paragraphs.append(paragraph)
    cleaned = "\n".join(cleaned_paragraphs)
    contaminated = bool(SOURCE_TEMPLATE_NOISE_RE.search(original))
    insufficient = len(_normalize_text(cleaned)) < 12
    return {
        "text": cleaned,
        "metrics": {
            "original_chars": len(original),
            "cleaned_chars": len(cleaned),
            "original_paragraphs": len(original_paragraphs),
            "cleaned_paragraphs": len(cleaned_paragraphs),
            "removed_noise_count": removed_noise_count,
            "duplicate_block_count": duplicate_block_count,
            "contamination_detected": contaminated,
            "source_quality_insufficient": insufficient,
        },
    }


def is_official_source(source: dict[str, Any]) -> bool:
    domain = str(source.get("domain") or urlparse(str(source.get("url") or "")).netloc).lower().split(":", 1)[0].strip(".")
    return str(source.get("source_level") or "") == "official" or domain in OFFICIAL_DOMAIN_WHITELIST or domain.endswith(".gov.cn") or domain.endswith(".gov")


def registrable_domain(domain: str) -> str:
    """Return a small dependency-free approximation of the public suffix domain."""
    host = str(domain or "").split(":", 1)[0].strip(".").lower()
    labels = [item for item in host.split(".") if item]
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in {"co", "com", "net", "org", "gov", "edu", "ac"}:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


class _SearchLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if values.get("href"):
            self.links.append(values["href"])


class _TextExtractor(HTMLParser):
    """Dependency-free extraction that prefers JSON-LD articleBody."""

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.meta: dict[str, str] = {}
        self.parts: list[str] = []
        self.jsonld_parts: list[str] = []
        self._skip = 0
        self._title = False
        self._jsonld = False
        self._dynamic_noise: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self._jsonld = True
            return
        if tag in {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "menu", "h1", "h2", "h3", "h4"}:
            self._skip += 1
        class_text = f"{values.get('class', '')} {values.get('id', '')}".lower()
        noise_tokens = ("recommend", "related", "comment", "sidebar", "footer", "nav", "video-list", "相关推荐", "热门推荐", "评论")
        if tag in {"div", "section", "ul", "ol", "aside", "footer", "nav"} and any(token in class_text for token in noise_tokens):
            self._skip += 1
            self._dynamic_noise.append(tag)
        if tag == "title":
            self._title = True
        if tag == "meta":
            key = values.get("property") or values.get("name")
            value = values.get("content")
            if key and value:
                self.meta[key.lower()] = value.strip()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script" and self._jsonld:
            self._jsonld = False
            return
        if tag == "title":
            self._title = False
        if self._dynamic_noise and self._dynamic_noise[-1] == tag:
            self._dynamic_noise.pop()
            if self._skip:
                self._skip -= 1
        if tag in {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "menu", "h1", "h2", "h3", "h4"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        value = re.sub(r"\s+", " ", data).strip()
        if not value:
            return
        if self._jsonld:
            self.jsonld_parts.append(data)
            return
        if self._skip:
            return
        if self._title:
            self.title = f"{self.title} {value}".strip()
        if len(value) >= 12:
            self.parts.append(value)


def _jsonld_article(parts: list[str]) -> dict[str, str]:
    for raw in parts:
        try:
            value = json.loads(raw.strip())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        if isinstance(value, dict) and isinstance(value.get("@graph"), list):
            candidates = value["@graph"]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            types = item.get("@type")
            types = types if isinstance(types, list) else [types]
            if not any(str(kind).lower() in {"newsarticle", "article", "report"} for kind in types):
                continue
            body = str(item.get("articleBody") or item.get("description") or "").strip()
            if body:
                return {"title": str(item.get("headline") or "").strip(), "body": body, "published_at": str(item.get("datePublished") or "").strip()}
    return {}


def _clean_paragraphs(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = re.sub(r"\s+", " ", str(value or "")).strip()
        normalized = _normalize_text(item)
        if len(item) < 12 or not normalized or normalized in seen or BOILERPLATE_RE.search(item) or PAGE_NOISE_RE.search(item):
            continue
        seen.add(normalized)
        result.append(item)
    return result


def extract_page_content(html: str, url: str, *, fetched_at: str | None = None) -> dict[str, Any]:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    jsonld = _jsonld_article(parser.jsonld_parts)
    if jsonld.get("body"):
        paragraphs = _clean_paragraphs(re.split(r"(<=[。！？；])\s*", jsonld["body"]))
        content_source = "jsonld_articleBody"
    else:
        paragraphs = _clean_paragraphs(parser.parts)
        content_source = "html_article_or_main"
    raw_content = "\n".join(paragraphs[:160])
    cleaned = clean_source_text(raw_content)
    content = str(cleaned.get("text") or "")
    audit = dict(cleaned.get("metrics") or {})
    parsed = urlparse(url)
    title = jsonld.get("title") or parser.title or parser.meta.get("og:title") or parser.meta.get("twitter:title") or url
    published = jsonld.get("published_at") or parser.meta.get("article:published_time") or parser.meta.get("datepublished") or parser.meta.get("date") or ""
    return {
        "title": title.strip(), "url": url, "canonical_url": canonical_url(url), "domain": parsed.netloc.lower(),
        "publisher_id": registrable_domain(parsed.netloc), "published_at": published, "fetched_at": fetched_at or _now(),
        "summary": content[:1800], "content": content, "content_hash": _content_fingerprint(content),
        "content_source": content_source, "fetch_success": len(content) >= 20 and not audit.get("source_quality_insufficient"), "source_level": "source_page",
        "source_cleaning": audit,
        "original_chars": audit.get("original_chars", len(raw_content)),
        "cleaned_chars": audit.get("cleaned_chars", len(content)),
        "original_paragraphs": audit.get("original_paragraphs", len(paragraphs)),
        "cleaned_paragraphs": audit.get("cleaned_paragraphs", len(paragraphs)),
        "removed_noise_count": audit.get("removed_noise_count", 0),
        "duplicate_block_count": audit.get("duplicate_block_count", 0),
        "source_quality_insufficient": audit.get("source_quality_insufficient", False),
        "source_contamination_detected": audit.get("contamination_detected", False),
    }


def _split_facts(text: str) -> list[str]:
    """Return complete, sentence-like body statements only; titles and page chrome are excluded."""
    values = re.findall(r"[^。！？；.!\n]{6,}[。！？；.!]", str(text or ""))
    result: list[str] = []
    for value in values:
        item = re.sub(r"\s+", " ", value).strip()
        if len(item) < 12 or PAGE_NOISE_RE.search(item) or BOILERPLATE_RE.search(item):
            continue
        result.append(item)
    return result[:60]


def _source_id(url: str, index: int) -> str:
    return f"source-{index}-{hashlib.sha1(canonical_url(url).encode('utf-8')).hexdigest()[:8]}"


def _similar_text(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalize_text(left)[:500], _normalize_text(right)[:500]).ratio()


_CONTRADICTORY_WORD_PAIRS = (
    ("缺席", "出席"), ("承认", "否认"), ("上涨", "下跌"), ("增加", "减少"),
    ("增长", "下降"), ("开放", "关闭"), ("通过", "否决"), ("上午", "下午"),
)
_CONTRADICTORY_RESULT_PAIRS = (("受伤", "无人受伤"), ("死亡", "无人死亡"))


def _number_signature(text: str) -> set[str]:
    without_dates = re.sub(r"20\d{2}年\s*\d{1,2}月\s*\d{1,2}日|\d{1,2}月\s*\d{1,2}日", "", str(text or ""))
    pattern = re.compile(r"\d+(?:\.\d+)?(?:亿元|万元|万|亿|元|人|%|公里|次|场|项)")
    return {match.group(0) for match in pattern.finditer(without_dates)}




def _fact_time(text: str) -> str:
    match = re.search(r"20\d{2}年\s*\d{1,2}月\s*\d{1,2}日|\d{1,2}月\s*\d{1,2}日|今天|昨日|明日|本周|当日", str(text or ""))
    return match.group(0) if match else ""


def _fact_location(text: str) -> str:
    match = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,20}(?:省|市|县|区|镇|村|路|街|站|机场|医院|学校|公司|园区|现场))", str(text or ""))
    return match.group(1) if match else ""


def _fact_number(text: str) -> str:
    match = re.search(r"\d+(?:\.\d+)?(?:亿元|万元|万|亿|元|人|%|公里|次|场|项)", str(text or ""))
    return match.group(0) if match else ""


def _fact_subject_action_object(text: str) -> tuple[str, str, str]:
    value = re.sub(r"\s+", " ", str(text or "")).strip(" ")
    for marker in FACT_ACTION_MARKERS:
        if marker in value:
            left, right = value.split(marker, 1)
            subject = left.strip(" ")[:24]
            obj = right.strip(" ")[:48]
            return subject, marker, obj
    return value[:24], "", value[24:72].strip(" ")


def _fact_reliability(record: dict[str, Any]) -> str:
    verification = str(record.get("verification_type") or "")
    if verification == "independent_publishers":
        return "cross_verified"
    if verification == "official_single_source":
        return "official"
    return "single_source"


def _is_background_fact(text: str) -> bool:
    return any(token in str(text or "") for token in ("背景", "原因", "此前", "曾经", "长期", "近年来", "历史", "相关规定"))


def _fact_cards(records: list[dict[str, Any]], sources: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    source_map = {str(item.get("source_id")): item for item in sources if item.get("source_id")}
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        fact_text = str(record.get("canonical_fact") or record.get("fact") or "").strip()
        normalized = _normalize_text(fact_text)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        source_ids = [str(value) for value in record.get("supporting_source_ids") or record.get("source_ids") or [] if str(value)]
        source = next((source_map.get(source_id) for source_id in source_ids if source_map.get(source_id)), {}) or {}
        subject, action, obj = _fact_subject_action_object(fact_text)
        cards.append({
            "fact_id": str(record.get("fact_id") or f"fact-{len(cards)+1}"),
            "fact": fact_text,
            "subject": subject,
            "action": action,
            "object": obj,
            "time": _fact_time(fact_text),
            "location": _fact_location(fact_text),
            "number": _fact_number(fact_text),
            "source_name": str(source.get("source_name") or source.get("publisher") or source.get("domain") or ""),
            "source_url": str(source.get("url") or ""),
            "reliability": _fact_reliability(record),
        })
        if len(cards) >= limit:
            break
    return cards
def _fact_conflicts(left: str, right: str) -> bool:
    """Minimal delivery-grade contradiction guard for similar facts."""
    left_text, right_text = str(left or ""), str(right or "")
    for positive, negative in _CONTRADICTORY_WORD_PAIRS:
        if (positive in left_text and negative in right_text) or (negative in left_text and positive in right_text):
            return True
    left_has, right_has = bool(re.search(r"(?<!没)有", left_text)), bool(re.search(r"(?<!没)有", right_text))
    left_no, right_no = "没有" in left_text, "没有" in right_text
    if (left_has and right_no) or (right_has and left_no):
        return True
    for result, negated in _CONTRADICTORY_RESULT_PAIRS:
        left_negated = negated in left_text or bool(re.search(r"(?:无|没有|无人)[^。！？；\n]{0,4}" + result, left_text))
        right_negated = negated in right_text or bool(re.search(r"(?:无|没有|无人)[^。！？；\n]{0,4}" + result, right_text))
        if result in left_text and result in right_text and left_negated != right_negated:
            return True
    left_numbers, right_numbers = _number_signature(left_text), _number_signature(right_text)
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return True
    return False


def _topic_terms(topic: Any) -> set[str]:
    text = f"{getattr(topic, 'title', '')} {getattr(topic, 'summary', '')}"
    meaningful_text = text
    for generic in GENERIC_TOPIC_TERMS:
        meaningful_text = meaningful_text.replace(generic, "")
    if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", meaningful_text):
        return set()
    terms: set[str] = set(re.findall(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        terms.update(run[index:index + size] for size in (2, 3, 4) for index in range(max(0, len(run) - size + 1)))
    return {term.lower() for term in terms if len(term) >= 2 and not any(generic in term for generic in GENERIC_TOPIC_TERMS)}


def _topic_entities(topic: Any) -> set[str]:
    text = f"{getattr(topic, 'title', '')} {getattr(topic, 'summary', '')}"
    suffix_pattern = re.compile(r"[\u4e00-\u9fff]{2,8}(?:" + "|".join(map(re.escape, ORG_SUFFIXES + PERSON_SUFFIXES)) + r")")
    entities = {match.group(0) for match in suffix_pattern.finditer(text)}
    # Short named entities such as 侯友宜 and company/product names are represented by 2-4 grams.
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        entities.update(run[index:index + size] for size in (2, 3, 4) for index in range(max(0, len(run) - size + 1)))
    return {item for item in entities if item not in GENERIC_TOPIC_TERMS and len(item) >= 2}


def score_source_relevance(topic: Any, source: dict[str, Any], *, threshold: float = 40.0) -> dict[str, Any]:
    title = str(source.get("title") or "")
    body = f"{source.get('content') or ''} {source.get('summary') or ''}"
    terms = _topic_terms(topic)
    entities = _topic_entities(topic)
    if not terms:
        return {"relevance_score": 50.0, "matched_topic_terms": [], "matched_entities": [], "rejection_reason": "", "accepted_for_research": bool(source.get("fetch_success"))}
    title_norm = _normalize_text(title)
    body_norm = _normalize_text(body)
    matched_title_terms = {term for term in terms if _normalize_text(term) in title_norm}
    matched_body_terms = {term for term in terms if _normalize_text(term) in body_norm}
    matched_entities = sorted(entity for entity in entities if _normalize_text(entity) in _normalize_text(f"{title} {body}"))
    matched = sorted(matched_title_terms | matched_body_terms)
    title_signal = bool(matched_title_terms or any(_normalize_text(entity) in title_norm for entity in matched_entities))
    body_signal = bool(matched_body_terms or matched_entities)
    score = min(100.0, (35.0 if title_signal else 0.0) + (35.0 if body_signal else 0.0) + min(30.0, len(matched) * 5.0))
    accepted = bool(source.get("fetch_success")) and score >= threshold and title_signal and body_signal
    if not source.get("fetch_success"):
        reason = "fetch_failed"
    elif not title_signal:
        reason = "title_entity_or_event_mismatch"
    elif not body_signal:
        reason = "body_entity_or_event_mismatch"
    elif score < threshold:
        reason = "relevance_below_threshold"
    else:
        reason = ""
    return {
        "relevance_score": round(score, 2), "matched_topic_terms": matched, "matched_entities": matched_entities,
        "rejection_reason": reason, "accepted_for_research": accepted,
    }


class ResearchService:
    def __init__(self, fetcher: Callable[[str], dict[str, Any]] | None = None, discoverer: Callable[[Any], Iterable[str]] | None = None) -> None:
        self.fetcher = fetcher or self.fetch_url
        self.discoverer = discoverer if discoverer is not None else (None if fetcher is not None else self.discover_urls)
        self.discovery_evidence: list[dict[str, Any]] = []

    @staticmethod
    def fetch_url(url: str) -> dict[str, Any]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {"url": url, "fetch_success": False, "error": "URL 格式不正确", "fetched_at": _now()}
        fetched_at = _now()
        try:
            with create_http_client({"timeout_seconds": 8}) as client:
                response = client.get(url, headers={"User-Agent": "Mozilla/5.0 hotspot-article-agent/rc1.3.2-r2", "Accept": "text/html,application/xhtml+xml"})
                response.raise_for_status()
            result = extract_page_content(response.text, url, fetched_at=fetched_at)
            result["source_name"] = parsed.netloc
            result["source_level"] = "official" if is_official_source(result) else result.get("source_level", "source_page")
            return result
        except Exception as exc:
            return {"url": url, "fetch_success": False, "error": str(exc)[:240], "fetched_at": fetched_at, "source_level": "source_page", "domain": parsed.netloc.lower(), "publisher_id": registrable_domain(parsed.netloc)}

    @staticmethod
    def _search_html(query: str, endpoint: str, name: str) -> list[str]:
        search_url = endpoint.format(query=quote_plus(query))
        with create_http_client({"timeout_seconds": 8}) as client:
            response = client.get(search_url, headers={"User-Agent": "Mozilla/5.0 hotspot-article-agent/rc1.3.2-r2", "Accept": "text/html,application/xhtml+xml"})
            response.raise_for_status()
        parser = _SearchLinkParser()
        parser.feed(response.text)
        found: list[str] = []
        for raw in parser.links:
            parsed = urlparse(raw)
            if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
                raw = parse_qs(parsed.query).get("uddg", [""])[0]
            parsed = urlparse(raw)
            if parsed.scheme in {"http", "https"} and parsed.netloc and not parsed.netloc.endswith(("duckduckgo.com", "bing.com")):
                found.append(canonical_url(raw))
        return list(dict.fromkeys(found))[:6]

    def primary_discoverer(self, topic: Any, query: str | None = None) -> list[str]:
        return self._search_html(query or str(getattr(topic, "title", "")), "https://html.duckduckgo.com/html/q={query}", "duckduckgo_html")

    def fallback_discoverer(self, topic: Any, query: str | None = None) -> list[str]:
        return self._search_html(query or str(getattr(topic, "title", "")), "https://www.bing.com/search?q={query}", "bing_html")

    def discover_with_fallback(self, topic: Any) -> tuple[list[str], list[dict[str, Any]]]:
        query = str(getattr(topic, "title", "") or "").strip()
        urls: list[str] = []
        evidence: list[dict[str, Any]] = []
        primary_error = ""
        try:
            found = self.primary_discoverer(topic, query)
            if not found:
                raise RuntimeError("NO_RESULTS")
            name = "primary_discoverer:duckduckgo_html"
            error_code = ""
        except Exception as exc:
            primary_error = str(exc)[:120] or "PRIMARY_SEARCH_FAILED"
            try:
                found = self.fallback_discoverer(topic, query)
                name = "fallback_discoverer:bing_html"
                error_code = "PRIMARY_SEARCH_FAILED"
            except Exception as fallback_exc:
                found = []
                name = "fallback_discoverer:bing_html"
                error_code = "PRIMARY_AND_FALLBACK_SEARCH_FAILED"
                primary_error = f"{primary_error}; {str(fallback_exc)[:100]}"
        urls.extend(found)
        evidence.append(
            {
                "discoverer_name": name,
                "query": query,
                "candidate_count": len(found),
                "accepted_count": 0,
                "rejected_count": 0,
                "error_code": error_code,
                "error_detail": primary_error,
            }
        )
        return list(dict.fromkeys(urls))[:6], evidence

    def discover_urls(self, topic: Any) -> list[str]:
        urls, evidence = self.discover_with_fallback(topic)
        self.discovery_evidence = evidence
        return urls

    @staticmethod
    def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        by_url: dict[str, str] = {}
        by_hash: dict[str, str] = {}
        for source in sources:
            source["canonical_url"] = canonical_url(str(source.get("url") or ""))
            source["publisher_id"] = source.get("publisher_id") or registrable_domain(str(source.get("domain") or urlparse(str(source.get("url") or "")).netloc))
            if not source.get("fetch_success") or not (source.get("content") or source.get("summary")):
                result.append(source)
                continue
            fingerprint = str(source.get("content_hash") or _content_fingerprint(str(source.get("content") or source.get("summary") or "")))
            duplicate_id = by_url.get(source["canonical_url"]) or by_hash.get(fingerprint)
            if not duplicate_id:
                same_story = next((item for item in result if item.get("fetch_success") and _similar_text(str(item.get("title")), str(source.get("title"))) >= 0.86 and _similar_text(str(item.get("content") or item.get("summary")), str(source.get("content") or source.get("summary"))) >= 0.96), None)
                duplicate_id = str((same_story or {}).get("source_id") or "")
            if duplicate_id:
                source["duplicate_of"] = duplicate_id
                source["fetch_success"] = False
                source["accepted_for_research"] = False
                source["rejection_reason"] = "duplicate_story"
                result.append(source)
                continue
            by_url[source["canonical_url"]] = str(source.get("source_id") or "")
            by_hash[fingerprint] = str(source.get("source_id") or "")
            source["content_hash"] = fingerprint
            result.append(source)
        return result

    @staticmethod
    def _facts(sources: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        valid = [source for source in sources if source.get("fetch_success") and source.get("accepted_for_research") and source.get("source_id")]
        sentences: list[tuple[dict[str, Any], str]] = []
        for source in valid:
            for fact in _split_facts(str(source.get("content") or source.get("summary") or "")):
                sentences.append((source, fact))
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source, fact in sentences:
            normalized = _normalize_text(fact)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            supporting = [str(other["source_id"]) for other, other_fact in sentences if _similar_text(fact, other_fact) >= 0.78]
            supporting = list(dict.fromkeys(supporting))
            publishers = list(dict.fromkeys(str(next(item for item in valid if item.get("source_id") == source_id).get("publisher_id") or "") for source_id in supporting))
            publishers = [item for item in publishers if item]
            official = any(str(item.get("source_id")) in supporting and (str(item.get("source_level")) == "official" or str(item.get("domain") or "").lower().endswith((".gov.cn", ".gov", ".edu.cn"))) for item in valid)
            if len(set(publishers)) >= 2:
                verification_type, confidence = "independent_publishers", "cross_verified"
            elif official:
                verification_type, confidence = "official_single_source", "official"
            else:
                verification_type, confidence = "single_source", "unverified"
            fact_id = f"fact-{hashlib.sha1(normalized.encode('utf-8')).hexdigest()[:12]}"
            record = {
                "fact_id": fact_id, "canonical_fact_id": fact_id, "fact": fact, "canonical_fact": fact,
                "source_ids": supporting, "supporting_source_ids": supporting, "supporting_publisher_ids": sorted(set(publishers)),
                "confidence": confidence, "verification_type": verification_type,
            }
            candidates.append(record)
        # Keep the complete canonical fact ledger for auditability.  Only facts with
        # independent/official verification are eligible to pass content quality.
        return candidates[:80], candidates[:120]

    @staticmethod
    def _facts_strict(sources: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Build three ledgers: verified, single-source, and every clean candidate."""
        valid = [source for source in sources if source.get("fetch_success") and source.get("accepted_for_research") and source.get("source_id")]
        sentences: list[tuple[dict[str, Any], str]] = []
        for source in valid:
            title_norm = _normalize_text(str(source.get("title") or ""))
            for fact in _split_facts(str(source.get("content") or "")):
                if title_norm and _similar_text(fact, str(source.get("title") or "")) >= 0.88:
                    continue
                sentences.append((source, fact))
        candidates: list[dict[str, Any]] = []
        verified: list[dict[str, Any]] = []
        single_source: list[dict[str, Any]] = []
        source_by_id = {str(item.get("source_id")): item for item in valid}
        clusters: list[list[tuple[dict[str, Any], str]]] = []
        for source, fact in sentences:
            normalized = _normalize_text(fact)
            if not normalized:
                continue
            matched_cluster = None
            for cluster in clusters:
                representative = cluster[0][1]
                if _similar_text(fact, representative) >= 0.82 and not _fact_conflicts(fact, representative):
                    matched_cluster = cluster
                    break
            if matched_cluster is None:
                clusters.append([(source, fact)])
            else:
                matched_cluster.append((source, fact))
        cluster_records: list[dict[str, Any]] = []
        for cluster in clusters:
            canonical_text = max((fact for _, fact in cluster), key=len)
            supporting = [str(source.get("source_id")) for source, _ in cluster]
            supporting = list(dict.fromkeys(supporting))
            publishers = sorted({str(source_by_id[source_id].get("publisher_id") or "") for source_id in supporting if source_id in source_by_id and source_by_id[source_id].get("publisher_id")})
            official_support = any(is_official_source(source_by_id[source_id]) for source_id in supporting if source_id in source_by_id)
            if len(publishers) >= 2:
                verification_type, confidence = "independent_publishers", "cross_verified"
            elif official_support:
                verification_type, confidence = "official_single_source", "official"
            else:
                verification_type, confidence = "single_source", "unverified"
            normalized = _normalize_text(canonical_text)
            fact_id = f"fact-{hashlib.sha1(normalized.encode('utf-8')).hexdigest()[:12]}"
            record = {
                "fact_id": fact_id,
                "canonical_fact_id": fact_id,
                "fact": canonical_text,
                "canonical_fact": canonical_text,
                "supporting_source_ids": supporting,
                "supporting_publisher_ids": publishers,
                "confidence": confidence,
                "verification_type": verification_type,
            }
            cluster_records.append(record)
        for index, record in enumerate(cluster_records):
            disputed = any(
                other_index != index and _similar_text(record["canonical_fact"], other["canonical_fact"]) >= 0.72 and _fact_conflicts(record["canonical_fact"], other["canonical_fact"])
                for other_index, other in enumerate(cluster_records)
            )
            if disputed:
                record["disputed"] = True
                record["dispute_reason"] = "相似事实存在相反词或关键数字冲突"
                record["verification_type"] = "single_source"
                record["confidence"] = "unverified"
            candidates.append(record)
            if record.get("verification_type") in {"independent_publishers", "official_single_source"} and not record.get("disputed"):
                verified.append(record)
            else:
                single_source.append(record)
        return verified[:80], single_source[:80], candidates[:160]

    @staticmethod
    def _clean_source_record(source: dict[str, Any]) -> dict[str, Any]:
        content = str(source.get("content") or source.get("summary") or "")
        result = dict(source)
        if isinstance(source.get("source_cleaning"), dict):
            metrics = dict(source.get("source_cleaning") or {})
            cleaned_text = content
        else:
            cleaned = clean_source_text(content)
            cleaned_text = str(cleaned.get("text") or "")
            metrics = dict(cleaned.get("metrics") or {})
        result["raw_source_chars"] = metrics.get("original_chars", len(content))
        result["source_cleaning"] = metrics
        result["original_chars"] = metrics.get("original_chars", len(content))
        result["cleaned_chars"] = metrics.get("cleaned_chars", len(cleaned_text))
        result["original_paragraphs"] = metrics.get("original_paragraphs", 0)
        result["cleaned_paragraphs"] = metrics.get("cleaned_paragraphs", 0)
        result["removed_noise_count"] = metrics.get("removed_noise_count", 0)
        result["duplicate_block_count"] = metrics.get("duplicate_block_count", 0)
        result["source_contamination_detected"] = bool(metrics.get("contamination_detected"))
        result["source_quality_insufficient"] = bool(metrics.get("source_quality_insufficient"))
        result["content"] = cleaned_text
        result["summary"] = cleaned_text[:1800]
        result["content_hash"] = _content_fingerprint(cleaned_text)
        if result["source_quality_insufficient"]:
            result["fetch_success"] = False
            result["accepted_for_research"] = False
            result["rejection_reason"] = "source_quality_insufficient"
        return result

    def collect(self, topic: Any, references: Iterable[str] | None = None, supplemental_text: str = "") -> dict[str, Any]:
        deadline = time.monotonic() + 60
        self.discovery_evidence = []
        urls: list[str] = [str(getattr(topic, "source_url", "") or "").strip()] if getattr(topic, "source_url", "") else []
        if self.discoverer is not None:
            discovered = self.discoverer(topic)
            if isinstance(discovered, dict):
                discovered_urls = discovered.get("urls") or []
                self.discovery_evidence = discovered.get("evidence") or []
            else:
                discovered_urls = discovered or []
                if not self.discovery_evidence:
                    self.discovery_evidence = [{"discoverer_name": "injected_discoverer", "query": str(getattr(topic, "title", "")), "candidate_count": len(discovered_urls), "accepted_count": 0, "rejected_count": 0, "error_code": ""}]
            urls.extend(str(value).strip() for value in discovered_urls if str(value).strip())
        urls.extend(str(value).strip() for value in references or [] if str(value).strip())
        urls = list(dict.fromkeys(canonical_url(url) for url in urls if url))
        raw_sources: list[dict[str, Any]] = []
        urls = urls[:8]
        for index, url in enumerate(urls, start=1):
            if time.monotonic() >= deadline:
                break
            source = dict(self.fetcher(url))
            source.setdefault("source_id", _source_id(url, index))
            source.setdefault("source_name", urlparse(url).netloc or "用户参考资料")
            source.setdefault("title", url)
            source.setdefault("summary", "")
            source.setdefault("content", "")
            source.setdefault("published_at", "")
            source.setdefault("fetched_at", _now())
            source.setdefault("canonical_url", canonical_url(url))
            source.setdefault("domain", urlparse(url).netloc.lower())
            source.setdefault("publisher_id", registrable_domain(str(source.get("domain") or urlparse(url).netloc)))
            source.setdefault("source_level", "source_page")
            source.setdefault("fetch_success", False)
            source = self._clean_source_record(source)
            if is_official_source(source):
                source["source_level"] = "official"
            source.update(score_source_relevance(topic, source))
            raw_sources.append(source)
            accepted_so_far = [item for item in raw_sources if item.get("fetch_success") and item.get("accepted_for_research")]
            official_so_far = [item for item in accepted_so_far if is_official_source(item)]
            media_so_far = [item for item in accepted_so_far if not is_official_source(item)]
            if len(accepted_so_far) >= 3 or (official_so_far and media_so_far):
                break
        if supplemental_text.strip():
            cleaned = clean_source_text(supplemental_text.strip())
            source = {"source_id": _source_id("supplemental", len(raw_sources) + 1), "source_name": "用户补充资料", "title": "用户粘贴资料", "url": "", "published_at": "", "fetched_at": _now(), "summary": str(cleaned.get("text") or "")[:1800], "content": str(cleaned.get("text") or ""), "source_level": "user_reference", "fetch_success": bool(str(cleaned.get("text") or "").strip()), "domain": "user", "publisher_id": "user", "source_cleaning": cleaned.get("metrics") or {}}
            source = self._clean_source_record(source)
            source.update(score_source_relevance(topic, source))
            raw_sources.append(source)
        
        # ── R1.2.1 补充搜索：hotlist_limited 时尝试搜索补充信息 ──
        accepted_count = len([s for s in raw_sources if s.get("fetch_success") and s.get("accepted_for_research")])
        if accepted_count == 0 and getattr(topic, "title", ""):
            # 最多尝试 3 个补充查询
            topic_title = str(getattr(topic, "title", "")).strip()
            # 去掉常见夸张词
            clean_title = re.sub(r'[？！!？\s]+', '', topic_title)
            clean_title = re.sub(r'(震惊|突发|刚刚|最新|重磅|紧急|速看|深度|真相|内幕|独家)', '', clean_title)
            # 构造搜索查询
            search_queries = [
                topic_title,
                re.sub(r'[！？，。、\s]+', ' ', clean_title)[:60].strip(),
            ]
            # 提取主要实体
            entities = re.findall(r'[\u4e00-\u9fff]{2,8}(?:公司|集团|政府|医院|学校|大学|部门|法院)', topic_title)
            if entities:
                search_queries.append(f"{entities[0]} 最新")
            elif ' ' in clean_title or '｜' in clean_title:
                parts = [p.strip() for p in re.split(r'[ ｜|]', clean_title) if p.strip()]
                if parts:
                    search_queries.append(f"{parts[0]} {' '.join(parts[1:2])}".strip())
            
            search_queries = list(dict.fromkeys(search_queries))[:3]
            supplemental_fetched = 0
            for sq in search_queries:
                if supplemental_fetched >= 3:
                    break
                if time.monotonic() >= deadline:
                    break
                try:
                    # 尝试通过 discoverer 做关键词搜索
                    if self.discoverer and callable(self.discoverer):
                        disc_result = self.discoverer(topic, query_override=sq)
                    else:
                        disc_result = None
                    extra_urls = []
                    if isinstance(disc_result, dict):
                        extra_urls = disc_result.get("urls") or []
                    elif isinstance(disc_result, list):
                        extra_urls = disc_result
                    for eu in extra_urls:
                        if supplemental_fetched >= 3:
                            break
                        if time.monotonic() >= deadline:
                            break
                        eu = str(eu).strip()
                        if not eu or eu in [s.get("url") for s in raw_sources]:
                            continue
                        source = dict(self.fetcher(eu))
                        source.setdefault("source_id", _source_id(eu, len(raw_sources) + 1))
                        source.setdefault("source_name", urlparse(eu).netloc or "补充搜索结果")
                        source.setdefault("title", eu)
                        source.setdefault("summary", "")
                        source.setdefault("content", "")
                        source.setdefault("published_at", "")
                        source.setdefault("fetched_at", _now())
                        source.setdefault("canonical_url", canonical_url(eu))
                        source.setdefault("domain", urlparse(eu).netloc.lower())
                        source.setdefault("publisher_id", registrable_domain(str(source.get("domain") or "")))
                        source.setdefault("source_level", "source_page")
                        source.setdefault("fetch_success", False)
                        source["supplemental_search"] = True
                        source = self._clean_source_record(source)
                        source.update(score_source_relevance(topic, source))
                        raw_sources.append(source)
                        if source.get("fetch_success") and source.get("accepted_for_research"):
                            supplemental_fetched += 1
                except Exception:
                    pass
        sources = self._dedupe_sources(raw_sources)
        for index, source in enumerate(sources, start=1):
            source["source_id"] = source.get("source_id") or _source_id(str(source.get("url") or source.get("title") or index), index)
        accepted_sources = [source for source in sources if source.get("fetch_success") and source.get("accepted_for_research") and not source.get("duplicate_of")]
        rejected_sources = [source for source in sources if not (source.get("fetch_success") and source.get("accepted_for_research") and not source.get("duplicate_of"))]
        facts, single_source_facts, candidate_facts = self._facts_strict(accepted_sources)
        disputed_facts = [fact for fact in single_source_facts if fact.get("disputed")]
        usable_facts = [fact for fact in list(facts) + list(single_source_facts) if not fact.get("disputed")]
        all_text = "\n".join(str(source.get("content") or source.get("summary") or "") for source in accepted_sources)
        timeline = [match.group(0) for match in re.finditer(r"(?:20\d{2}年\s*\d{1,2}月\s*\d{1,2}日|\d{1,2}月\s*\d{1,2}日|今天|昨日|明日|本周|当日)", all_text)][:30]
        numbers = [match.group(0) for match in re.finditer(r"\d+(?:\.\d+)?(?:亿元|万元|万|亿|元|人|%|公里|次|场|项)", all_text)][:30]
        key_people = list(dict.fromkeys(match.group(0) for match in re.finditer(r"[\u4e00-\u9fff]{2,8}(?:先生|女士|市长|部长|局长|总统|议员)", all_text)))[:20]
        key_orgs = list(dict.fromkeys(match.group(0) for match in re.finditer(r"[\u4e00-\u9fff]{2,12}(?:公司|集团|政府|市政府|委员会|学校|医院|协会|大学|部门|会议)", all_text)))[:20]
        background = [str(source.get("summary") or "") for source in accepted_sources if source.get("summary")][:5]
        publishers = sorted({str(source.get("publisher_id") or "") for source in accepted_sources if str(source.get("publisher_id") or "") not in {"", "user"}})
        cross_verified = sum(1 for fact in facts if fact.get("verification_type") == "independent_publishers")
        official_sources = [source for source in accepted_sources if is_official_source(source)]
        official = len(official_sources)
        official_fact_count = sum(1 for fact in facts if fact.get("verification_type") == "official_single_source")
        reliable_source_count = len([source for source in accepted_sources if is_official_source(source) or str(source.get("source_level") or "") in {"official", "source_page"}])
        usable_fact_count = len(usable_facts)
        source_cleaning_summary = {
            "raw_source_count": len(raw_sources),
            "cleaned_source_count": len([source for source in sources if int(source.get("cleaned_chars") or 0) > 0]),
            "accepted_cleaned_source_count": len(accepted_sources),
            "contaminated_source_count": len([source for source in sources if source.get("source_contamination_detected")]),
            "insufficient_source_count": len([source for source in sources if source.get("source_quality_insufficient")]),
            "removed_noise_count": sum(int(source.get("removed_noise_count") or 0) for source in sources),
            "duplicate_block_count": sum(int(source.get("duplicate_block_count") or 0) for source in sources),
        }
        research_fact_cards = _fact_cards(usable_facts, accepted_sources)
        background_fact_cards = _fact_cards(
            [f for f in usable_facts if _is_background_fact(f.get("canonical_fact", "") or f.get("fact", ""))],
            accepted_sources,
        )
        has_event_context = bool(key_people or key_orgs or timeline or getattr(topic, "title", ""))
        has_conflict = bool(disputed_facts)
        condition_a = len(accepted_sources) >= 2
        condition_b = reliable_source_count >= 1
        score = min(100, len(accepted_sources) * 15 + min(35, usable_fact_count * 5) + (15 if timeline else 0) + (10 if key_people or key_orgs else 0) + (15 if background else 0) + (10 if cross_verified or official else 0))
        status = "insufficient" if not accepted_sources else "sufficient" if (condition_a or condition_b) else "limited"
        insufficient_reasons: list[str] = []
        if not accepted_sources:
            insufficient_reasons.append("没有找到相关公开资料")
        if not has_event_context:
            insufficient_reasons.append("缺少明确人物、机构或事件经过")
        if not condition_a and not condition_b:
            insufficient_reasons.append("可用公开资料较少")
        if has_conflict:
            insufficient_reasons.append("多个来源存在明显互相矛盾的信息")
        discovery = list(self.discovery_evidence or [])
        total_candidates = len(urls)
        total_rejected = len(rejected_sources)
        for item in discovery:
            item["accepted_count"] = len(accepted_sources)
            item["rejected_count"] = total_rejected
            item["candidate_count"] = max(int(item.get("candidate_count") or 0), total_candidates)
        bundle = {
            "topic_id": topic.id, "topic_title": topic.title,
            "source_page": sources[0] if sources else {"url": getattr(topic, "source_url", ""), "title": topic.title, "fetch_success": False},
            "sources": sources, "rejected_sources": rejected_sources, "verified_facts": facts, "single_source_facts": single_source_facts, "candidate_facts": candidate_facts, "usable_facts": usable_facts, "disputed_facts": disputed_facts,
            "research_fact_cards": research_fact_cards, "background_fact_cards": background_fact_cards,
            "timeline": timeline, "key_people": key_people, "key_organizations": key_orgs, "locations": [], "numbers": numbers,
            "official_statements": [source.get("source_id") for source in official_sources], "background": background, "disagreements": [], "unknowns": [],
            "unique_source_domains": publishers, "unique_publisher_ids": publishers, "independent_publisher_count": len(publishers),
            "cross_verified_fact_count": cross_verified, "official_source_count": official, "official_fact_count": official_fact_count, "official_or_reliable_source_count": reliable_source_count, "usable_fact_count": usable_fact_count, "discovery": discovery,
            "discovery_evidence": discovery, "candidate_link_count": total_candidates, "accepted_source_count": len(accepted_sources),
            "rejected_source_count": total_rejected, "search_failure_visible_to_user": any(str(item.get("error_code") or "") for item in discovery),
            "research_status": status, "information_sufficiency_score": score, "insufficient_reasons": insufficient_reasons,
            "source_cleaning_summary": source_cleaning_summary,
            "minimum_gate": {"condition_a": condition_a, "condition_b": condition_b, "has_event_context": has_event_context, "has_conflict": has_conflict},
            "collected_at": _now(),
        }
        path = research_root() / topic.id / "research_bundle.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return bundle


def load_research_bundle(topic_id: str) -> dict[str, Any] | None:
    path = research_root() / str(topic_id) / "research_bundle.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None

