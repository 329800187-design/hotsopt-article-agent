from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any

from generation.image_budget import count_body_chinese_chars
from modules.source_formatter import normalize_source_list


VAGUE_PATTERNS = ("目前无法确认", "现有信息没有说明", "尚不能判断", "需要等待后续信息", "不宜扩大解读")

# ── R1.2.1 空话检测词表 ──
UNKNOWN_PHRASES = (
    "尚未确认", "仍待核实", "公开信息有限", "无法判断", "不能判断",
    "等待权威", "暂无资料", "未提供", "仍需等待", "后续关注",
    "有待观察", "目前尚不", "还没有更多", "仍不清楚", "不得而知",
    "尚不明确", "正在核实", "信息有限",
)
VALUE_SECTION_MARKERS = {
    "核验路径": ("核验", "查证", "验证", "核实", "鉴定"),
    "传播风险": ("风险", "误读", "谣言", "误导", "虚假"),
    "背景解释": ("背景", "原因", "起因", "来龙去脉", "前因"),
    "同类案例": ("案例", "类似", "此前", "过去", "参考"),
    "普通读者启示": ("普通人", "读者", "启示", "启发", "教训", "提醒"),
    "影响分析": ("影响", "后果", "波及", "连锁", "效应"),
    "明确观点": ("我认为", "这说明", "意味着", "可以看出", "退一步说", "问题是"),
}
TIME_RE = re.compile(r"(?:20\d{2}年\s*\d{1,2}月\s*\d{1,2}日|\d{1,2}月\s*\d{1,2}日|今天|昨日|明日|本周|当日)")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?(?:亿元|万元|万|亿|元|人|%|公里|次|场|项)")
CONCRETE_CLAIM_RE = re.compile(
    r"(?:\d+(?:\.\d+)?(?:亿元|万元|万|亿|元|人|%|公里|次|场|项)|"
    r"20\d{2}年\s*\d{1,2}月\s*\d{1,2}日|\d{1,2}月\s*\d{1,2}日|"
    r"(?:辞职|逮捕|关闭|发布(?!主体|渠道|平台|节奏|日期|时间|前)|上涨|下跌|增加|减少|缺席|出席|被判|判处|处罚|罚款|死亡|去世|受伤|入院|损失))"
)
EXPLICIT_NONFACT_RE = re.compile(r"(?:\[\s*(?:analysis|unknown|disputed)\s*\]|(?:analysis|unknown|disputed)\s*[:：]|分析\s*[:：]|未知\s*[:：]|争议\s*[:：])", re.I)
CLAUSE_SPLIT_RE = re.compile(r"[，,；;、]+|并且|同时|此外|导致|造成|因此|且|并")
HARD_FACT_WARNING_RE = CONCRETE_CLAIM_RE
SOFT_ANALYSIS_RE = re.compile(r"(?:可能|值得关注|从现有资料看|这意味着|趋势|影响|观点|分析|或许|有待观察|后续|折射|显示出|提醒)")
SOURCE_SECTION_TITLES = {"资料来源", "参考资料", "信息来源", "来源列表"}
SOURCE_REF_LINE_RE = re.compile(r"^\s*(?:\[\s*\d+\s*\]|\d+\s*[.．、])\s*.+")
URL_RE = re.compile(r"https://|www\.", re.I)
PUBLISHED_AT_ONLY_RE = re.compile(r"^\s*(?:发布时间|发布日期|来源时间)\s*[:：]\s*(?:20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日|\d{1,2}月\d{1,2}日)(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*$")
NEGATION_MARKERS = ("并不存在", "没有发生", "并未", "尚未", "未曾", "没有", "否认", "未", "不", "无")
POLARITY_ACTIONS = (
    "辞职", "逮捕", "关闭", "发布", "上涨", "下跌", "增加", "减少",
    "缺席", "出席", "被判", "判处", "处罚", "罚款", "死亡", "去世",
    "受伤", "入院", "损失", "投入",
)


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。！？；\n]+", str(text or "")) if part.strip()]


def _fact_clauses(text: str) -> list[str]:
    """Split compound sentences before checking concrete claims."""
    clauses: list[str] = []
    for sentence in _sentences(text):
        clauses.extend(part.strip() for part in CLAUSE_SPLIT_RE.split(sentence) if part.strip())
    return clauses


def _normalize(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "")).lower()


SOURCE_CONTAMINATION_RE = re.compile(
    r"\{\{|\}\}|dynamicData|subjectData|item\.reporter_name|item\.tag|reporter_name|"
    r"未发布文章|文章未发布|仅支持\s*15\s*分钟预览|后台刷新重置预览|"
    r"打开[\u4e00-\u9fffA-Za-z0-9]{0,12}新闻|阅读体验更佳|更多内容请打开|下载\s*APP|APP内打开"
)
FIXED_FILLER_HEADINGS = {"事件概览", "已确认信息", "背景信息", "可能影响", "后续关注"}
DANGLING_SECOND_MARKER_RE = re.compile(r"(?:^|[。！？；;:：\n]\s*)(?:二是|其二|第二，|第二、|第二点)")
FIRST_MARKER_RE = re.compile(r"(?:^|[。！？；;:：\n]\s*)(?:一是|其一|第一，|第一、|第一点)")
PHONE_DROP_SOURCE_RE = re.compile(r"(?:手机|iPhone|设备).{0,30}(?:坠落|掉落)|(?:坠落|掉落).{0,30}(?:手机|iPhone|设备)")
AIRCRAFT_ACCIDENT_WORDING_RE = re.compile(r"(?:坠机|空难|飞机失事|坠毁)")
LOW_KM_SOURCE_RE = re.compile(r"(?:1(?:\.\d+)?|2(?:\.\d+)?)\s*(?:千米|公里|km|KM)")
TEN_THOUSAND_METER_RE = re.compile(r"(?:万米|一万米|10000\s*米|10,000\s*米)")
UNSUPPORTED_TECH_DETAIL_RE = re.compile(r"(?:卫星信号|航空级铝合金|超瓷晶|陶瓷护盾|钛金属边框|主板焊点|电池鼓包|防抖组件)")
FOLLOWUP_NONFACT_RE = re.compile(r"(?:建议|后续|是否|可能|等待|关注).{0,40}(?:发布|说明|回应|结论|来源|信息|下结论)")


def contamination_hits(text: str) -> list[str]:
    hits = [match.group(0) for match in SOURCE_CONTAMINATION_RE.finditer(str(text or ""))]
    return list(dict.fromkeys(hits))[:20]


def dangling_list_marker_hits(text: str) -> list[str]:
    content = str(text or "")
    if not DANGLING_SECOND_MARKER_RE.search(content) or FIRST_MARKER_RE.search(content):
        return []
    return [match.group(0).strip() for match in DANGLING_SECOND_MARKER_RE.finditer(content)][:5]


def misleading_aircraft_accident_hits(article: dict[str, Any], research_bundle: dict[str, Any] | None) -> list[str]:
    markdown = str(article.get("content_markdown") or "")
    if not AIRCRAFT_ACCIDENT_WORDING_RE.search(markdown):
        return []
    bundle = research_bundle or {}
    source_text = " ".join(
        str(item.get("title") or "") + " " + str(item.get("content") or item.get("text") or "")
        for item in bundle.get("sources") or []
        if isinstance(item, dict)
    )
    topic_text = str(bundle.get("topic_title") or bundle.get("title") or "")
    context = f"{topic_text} {source_text}"
    if PHONE_DROP_SOURCE_RE.search(context) and not AIRCRAFT_ACCIDENT_WORDING_RE.search(source_text):
        return list(dict.fromkeys(match.group(0) for match in AIRCRAFT_ACCIDENT_WORDING_RE.finditer(markdown)))[:5]
    return []


def exaggerated_altitude_hits(article: dict[str, Any], research_bundle: dict[str, Any] | None) -> list[str]:
    markdown = str(article.get("content_markdown") or "")
    if not TEN_THOUSAND_METER_RE.search(markdown):
        return []
    bundle = research_bundle or {}
    source_text = " ".join(
        str(item.get("title") or "") + " " + str(item.get("content") or item.get("text") or "")
        for item in bundle.get("sources") or []
        if isinstance(item, dict)
    )
    if LOW_KM_SOURCE_RE.search(source_text) and not TEN_THOUSAND_METER_RE.search(source_text):
        return list(dict.fromkeys(match.group(0) for match in TEN_THOUSAND_METER_RE.finditer(markdown)))[:5]
    return []


def unsupported_technical_detail_hits(article: dict[str, Any], research_bundle: dict[str, Any] | None) -> list[str]:
    markdown = str(article.get("content_markdown") or "")
    bundle = research_bundle or {}
    source_text = " ".join(
        str(item.get("title") or "") + " " + str(item.get("content") or item.get("text") or "")
        for item in bundle.get("sources") or []
        if isinstance(item, dict)
    )
    hits = []
    for match in UNSUPPORTED_TECH_DETAIL_RE.finditer(markdown):
        value = match.group(0)
        if value and value not in source_text:
            hits.append(value)
    return list(dict.fromkeys(hits))[:5]


def _article_title(article: dict[str, Any]) -> str:
    title = str(article.get("title") or "").strip()
    if title:
        return re.sub(r"^#{1,6}\s*", "", title).strip()
    match = re.search(r"^#\s*(.+)$", str(article.get("content_markdown") or ""), flags=re.M)
    return match.group(1).strip() if match else ""


def unbalanced_title_quote_hits(article: dict[str, Any]) -> list[str]:
    title = _article_title(article)
    if not title:
        return []
    pairs = (("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"), ("《", "》"))
    for left, right in pairs:
        if title.count(left) != title.count(right):
            return [title]
    if title.count('"') % 2 or title.count("'") % 2:
        return [title]
    return []


def copied_source_title_hits(article: dict[str, Any], research_bundle: dict[str, Any] | None) -> list[str]:
    title = _article_title(article)
    title_norm = _normalize(title)
    if len(title_norm) < 14:
        return []
    bundle = research_bundle or {}
    candidates = [
        str(bundle.get("topic_title") or bundle.get("title") or "").strip(),
    ]
    for source in bundle.get("sources") or []:
        if isinstance(source, dict):
            candidates.append(str(source.get("title") or "").strip())
    hits: list[str] = []
    for candidate in candidates:
        candidate_norm = _normalize(candidate)
        if len(candidate_norm) < 14:
            continue
        min_len = min(len(title_norm), len(candidate_norm))
        max_len = max(len(title_norm), len(candidate_norm))
        if title_norm == candidate_norm:
            hits.append(candidate)
            continue
        if min_len / max(1, max_len) >= 0.75 and (title_norm in candidate_norm or candidate_norm in title_norm):
            hits.append(candidate)
            continue
        if SequenceMatcher(None, title_norm, candidate_norm).ratio() >= 0.92:
            hits.append(candidate)
    return list(dict.fromkeys(item for item in hits if item))[:5]


def _body_paragraphs(markdown: str) -> list[str]:
    lines: list[str] = []
    for raw in str(markdown or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        if re.match(r"^#\s+", line):
            continue
        if re.match(r"^#{2,6}\s+", line):
            lines.append("")
            continue
        lines.append(line)
    blocks = [item.strip() for item in re.split(r"\n\s*\n+", "\n".join(lines)) if item.strip()]
    return [item for item in blocks if len(_normalize(item)) >= 10]


def _sentence_list(markdown: str) -> list[str]:
    body = "\n".join(_body_paragraphs(markdown))
    return [item.strip() for item in re.split(r"[。！？；.!?\n]+", body) if len(_normalize(item)) >= 12]


def _repeated_chinese_fragments(text: str, size: int = 30) -> list[str]:
    compact = "".join(re.findall(r"[\u4e00-\u9fff]", str(text or "")))
    seen: set[str] = set()
    repeats: list[str] = []
    for index in range(0, max(0, len(compact) - size + 1)):
        fragment = compact[index:index + size]
        if fragment in seen and fragment not in repeats:
            repeats.append(fragment)
            if len(repeats) >= 5:
                break
        seen.add(fragment)
    return repeats


def _ngram_similarity(left: str, right: str, n: int) -> float:
    a = _normalize(left)
    b = _normalize(right)
    if len(a) < n or len(b) < n:
        return 0.0
    grams_a = {a[index:index + n] for index in range(len(a) - n + 1)}
    grams_b = {b[index:index + n] for index in range(len(b) - n + 1)}
    return len(grams_a & grams_b) / max(1, min(len(grams_a), len(grams_b)))


def intra_article_quality(article: dict[str, Any], research_bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    markdown = str(article.get("content_markdown") or "")
    paragraphs = _body_paragraphs(markdown)
    sentences = _sentence_list(markdown)
    heading_hits = re.findall(r"^#{2,6}\s*(.+)$", markdown, flags=re.M)
    exact_repeats = [
        paragraph
        for paragraph, count in Counter(_normalize(item) for item in paragraphs if _normalize(item)).items()
        if count > 1
    ]
    normalized_sentence_repeats = [
        sentence
        for sentence, count in Counter(_normalize(item) for item in sentences if len(_normalize(item)) >= 24).items()
        if count > 1
    ]
    similar_pairs: list[dict[str, Any]] = []
    max_similarity = 0.0
    for left_index in range(len(paragraphs)):
        for right_index in range(left_index + 1, len(paragraphs)):
            left = paragraphs[left_index]
            right = paragraphs[right_index]
            seq = SequenceMatcher(None, _normalize(left), _normalize(right)).ratio()
            gram = max(_ngram_similarity(left, right, 3), _ngram_similarity(left, right, 4))
            similarity = max(seq, gram)
            max_similarity = max(max_similarity, similarity)
            if similarity > 0.82:
                similar_pairs.append({"left": left_index + 1, "right": right_index + 1, "similarity": round(similarity, 4)})
    incomplete = [
        item
        for item in sentences
        if re.search(r"(进入了\s*[，,]\s*就|可能进入了\s*[，,]|[，,]\s*就没办法|[的了在将把被与]\s*[。！？]$)", item)
    ][:5]
    dangling_markers = dangling_list_marker_hits(markdown)
    aircraft_wording = misleading_aircraft_accident_hits(article, research_bundle)
    exaggerated_altitude = exaggerated_altitude_hits(article, research_bundle)
    unsupported_tech = unsupported_technical_detail_hits(article, research_bundle)
    unbalanced_quotes = unbalanced_title_quote_hits(article)
    copied_titles = copied_source_title_hits(article, research_bundle)
    repeated_fragments = _repeated_chinese_fragments(markdown)
    fixed_heading_count = sum(1 for heading in heading_hits if heading.strip() in FIXED_FILLER_HEADINGS)
    failures: list[str] = []
    if exact_repeats:
        failures.append("REPEATED_PARAGRAPH")
    if normalized_sentence_repeats:
        failures.append("REPEATED_SENTENCE")
    if repeated_fragments:
        failures.append("REPEATED_LONG_FRAGMENT")
    if similar_pairs:
        failures.append("SIMILAR_PARAGRAPHS")
    if contamination_hits(markdown):
        failures.append("SOURCE_CONTENT_CONTAMINATED")
    if incomplete:
        failures.append("INCOMPLETE_SENTENCE")
    if dangling_markers:
        failures.append("DANGLING_LIST_MARKER")
    if aircraft_wording:
        failures.append("MISLEADING_AIRCRAFT_ACCIDENT_WORDING")
    if exaggerated_altitude:
        failures.append("EXAGGERATED_ALTITUDE_WORDING")
    if unsupported_tech:
        failures.append("UNSUPPORTED_TECHNICAL_DETAIL")
    if unbalanced_quotes:
        failures.append("UNBALANCED_TITLE_QUOTE")
    if copied_titles:
        failures.append("COPIED_SOURCE_TITLE")
    if fixed_heading_count >= 4:
        failures.append("FIXED_FILLER_STRUCTURE")
    return {
        "passed": not failures,
        "failures": list(dict.fromkeys(failures)),
        "exact_repeated_paragraph_count": len(exact_repeats),
        "normalized_repeated_sentence_count": len(normalized_sentence_repeats),
        "repeated_long_fragments": repeated_fragments,
        "similar_paragraph_pairs": similar_pairs[:10],
        "max_paragraph_similarity": round(max_similarity, 4),
        "contamination_hits": contamination_hits(markdown),
        "incomplete_sentences": incomplete,
        "dangling_list_marker_hits": dangling_markers,
        "misleading_aircraft_accident_hits": aircraft_wording,
        "exaggerated_altitude_hits": exaggerated_altitude,
        "unsupported_technical_detail_hits": unsupported_tech,
        "unbalanced_title_quote_hits": unbalanced_quotes,
        "copied_source_title_hits": copied_titles,
        "fixed_filler_heading_count": fixed_heading_count,
    }


def _article_body_for_fact_scan(markdown: str) -> str:
    """Return article body only; source/reference sections are not factual claims."""
    body_lines: list[str] = []
    for raw_line in str(markdown or "").splitlines():
        line = raw_line.strip()
        if re.match(r"^#{1,6}\s+", line):
            continue
        heading = re.sub(r"^[#>\-\s]+", "", line).strip().rstrip("：:")
        if heading in SOURCE_SECTION_TITLES:
            break
        if "AI辅助声明" in line or "AI声明" in line or "免责声明" in line:
            continue
        if SOURCE_REF_LINE_RE.match(line) or URL_RE.search(line) or PUBLISHED_AT_ONLY_RE.match(line):
            continue
        body_lines.append(raw_line)
    return "\n".join(body_lines)


def _tokens(value: str) -> set[str]:
    text = _normalize(value)
    if len(text) <= 2:
        return {text} if text else set()
    return {text[index:index + 2] for index in range(len(text) - 1)}


def _fact_supported(fact: str, source_text: str) -> bool:
    normalized_fact = _normalize(fact)
    normalized_source = _normalize(source_text)
    if not normalized_fact or not normalized_source:
        return False
    if normalized_fact in normalized_source:
        return True
    left, right = _tokens(fact), _tokens(source_text)
    return bool(left) and len(left & right) / len(left) >= 0.72


def _number_signature(text: str) -> set[str]:
    pattern = re.compile(
        r"(?:20\d{2}年\s*\d{1,2}月\s*\d{1,2}日|\d{1,2}月\s*\d{1,2}日|"
        r"\d+(?:\.\d+)?(?:亿元|万元|万|亿|元|人|%|公里|次|场|项))"
    )
    return {match.group(0) for match in pattern.finditer(str(text or ""))}


def _action_polarities(text: str) -> dict[str, bool]:
    """Return whether each concrete action is negated in nearby text.

    The negation window intentionally looks only before the action phrase.  This
    prevents words such as “无期徒刑” from turning “被判无期徒刑” into a
    negative claim, while still catching “未被判无期徒刑”.
    """
    compact = re.sub(r"\s+", "", str(text or ""))
    polarities: dict[str, bool] = {}
    for action in POLARITY_ACTIONS:
        start = 0
        while True:
            index = compact.find(action, start)
            if index < 0:
                break
            window = compact[max(0, index - 12):index + len(action)]
            negated = any(marker in window for marker in NEGATION_MARKERS)
            if action not in polarities:
                polarities[action] = negated
            else:
                polarities[action] = polarities[action] and negated
            start = index + len(action)
    return polarities


def _polarity_conflicts(claim_clause: str, canonical_fact: str) -> bool:
    claim_polarities = _action_polarities(claim_clause)
    fact_polarities = _action_polarities(canonical_fact)
    for action in set(claim_polarities) & set(fact_polarities):
        if claim_polarities[action] != fact_polarities[action]:
            return True
    return False


def claim_supported_by_fact(claim_clause: str, canonical_fact: str) -> bool:
    """Check whether a concrete article clause is covered by a canonical fact.

    The shorter claim clause is the denominator, so a date-only or amount-only
    subclause can be supported by a longer canonical fact.  Numeric/date
    signatures in the claim must still be present in the canonical fact.  If
    the claim and fact describe the same concrete action with opposite polarity,
    reject before substring or fuzzy matching.
    """
    claim_norm = _normalize(claim_clause)
    fact_norm = _normalize(canonical_fact)
    if not claim_norm or not fact_norm:
        return False
    if _polarity_conflicts(claim_clause, canonical_fact):
        return False
    claim_numbers = _number_signature(claim_clause)
    fact_numbers = _number_signature(canonical_fact)
    if claim_numbers and not claim_numbers.issubset(fact_numbers):
        return False
    if claim_norm in fact_norm or fact_norm in claim_norm:
        return True
    claim_tokens, fact_tokens = _tokens(claim_clause), _tokens(canonical_fact)
    return bool(claim_tokens) and len(claim_tokens & fact_tokens) / len(claim_tokens) >= 0.72


def _canonical_fact_map(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    facts = list(bundle.get("usable_facts") or [])
    if not facts:
        facts = list(bundle.get("verified_facts") or []) + list(bundle.get("single_source_facts") or [])
    for item in facts:
        if not isinstance(item, dict):
            continue
        fact_id = str(item.get("fact_id") or item.get("canonical_fact_id") or "").strip()
        canonical = str(item.get("canonical_fact") or item.get("fact") or "").strip()
        if fact_id and canonical:
            result[fact_id] = item
    # R2.2.7 is positioned as a public-source assisted writing tool, not a
    # professional fact-ledger system.  If the simplified research flow has not
    # produced canonical facts, hard factual claims may still pass when the exact
    # claim is supported by accepted source text.  Fabricated numbers, penalties,
    # resignations, injuries, etc. still fail because they will not be found in
    # these source-backed support texts.
    for index, source in enumerate(bundle.get("sources") or [], start=1):
        if not isinstance(source, dict):
            continue
        if source.get("accepted_for_research") is False or source.get("fetch_success") is False:
            continue
        source_text = str(source.get("content") or source.get("text") or source.get("title") or "").strip()
        if not source_text:
            continue
        source_id = str(source.get("source_id") or f"source-{index}")
        result.setdefault(
            f"source:{source_id}",
            {
                "fact_id": f"source:{source_id}",
                "canonical_fact": source_text,
                "verification_type": "source_text",
                "supporting_source_ids": [source_id],
            },
        )
    return result


def _single_source_attributed(markdown: str, canonical: dict[str, Any], source_map: dict[str, dict[str, Any]]) -> bool:
    if str(canonical.get("verification_type") or "") != "single_source":
        return True
    text = str(markdown or "")
    if any(marker in text for marker in ("据", "根据", "公开资料显示", "目前公开资料显示", "从现有资料看")):
        return True
    for source_id in canonical.get("supporting_source_ids") or canonical.get("source_ids") or []:
        source = source_map.get(str(source_id))
        if not source:
            continue
        source_name = str(source.get("source_name") or source.get("publisher_id") or source.get("domain") or "").strip()
        if source_name and source_name in text:
            return True
    return False


def _unsupported_concrete_claims(markdown: str, fact_map: dict[str, dict[str, Any]]) -> list[str]:
    """Find concrete article statements that are absent from verified canonical facts."""
    unsupported: list[str] = []
    canonical_facts = [str(item.get("canonical_fact") or item.get("fact") or "") for item in fact_map.values()]
    body = _article_body_for_fact_scan(markdown)
    for clause in _fact_clauses(body):
        if not CONCRETE_CLAIM_RE.search(clause) or EXPLICIT_NONFACT_RE.search(clause):
            continue
        if FOLLOWUP_NONFACT_RE.search(clause):
            continue
        if SOURCE_REF_LINE_RE.match(clause) or URL_RE.search(clause) or PUBLISHED_AT_ONLY_RE.match(clause):
            continue
        if not any(claim_supported_by_fact(clause, fact) for fact in canonical_facts if fact.strip()):
            unsupported.append(clause[:240])
    return list(dict.fromkeys(unsupported))[:20]


def _soft_analysis_warning_clauses(markdown: str) -> list[str]:
    warnings: list[str] = []
    body = _article_body_for_fact_scan(markdown)
    for clause in _fact_clauses(body):
        if EXPLICIT_NONFACT_RE.search(clause):
            continue
        if CONCRETE_CLAIM_RE.search(clause):
            continue
        if SOFT_ANALYSIS_RE.search(clause):
            warnings.append(clause[:240])
    return list(dict.fromkeys(warnings))[:20]


def validate_fact_basis(article: dict[str, Any], research_bundle: dict[str, Any] | None) -> dict[str, Any]:
    """Validate model citations against the immutable, source-backed fact bundle."""
    bundle = research_bundle or {}
    source_map = {
        str(item.get("source_id")): item
        for item in bundle.get("sources") or []
        if isinstance(item, dict) and item.get("source_id") and item.get("fetch_success") and item.get("accepted_for_research") and not item.get("duplicate_of")
    }
    fact_map = _canonical_fact_map(bundle)
    facts = article.get("fact_basis")
    if not isinstance(facts, list) or not facts:
        return {"valid": False, "validated_count": 0, "total_count": 0, "source_coverage": 0.0, "cross_verified_count": 0, "invalid_reasons": ["文章没有返回可追溯的 fact_basis"]}
    unique_facts: list[Any] = []
    seen_fact_ids: set[str] = set()
    duplicate_fact_ids: set[str] = set()
    for item in facts:
        if isinstance(item, dict):
            item_id = str(item.get("fact_id") or item.get("canonical_fact_id") or "").strip()
            if item_id and item_id in seen_fact_ids:
                duplicate_fact_ids.add(item_id)
                continue
            if item_id:
                seen_fact_ids.add(item_id)
        unique_facts.append(item)
    validated = 0
    cross_verified = 0
    invalid: list[str] = []
    markdown = str(article.get("content_markdown") or "")
    for index, item in enumerate(unique_facts, start=1):
        prefix = f"第 {index} 条事实"
        if not isinstance(item, dict):
            invalid.append(f"{prefix}不是对象")
            continue
        fact_id = str(item.get("fact_id") or item.get("canonical_fact_id") or "").strip()
        canonical = fact_map.get(fact_id)
        if not fact_id or canonical is None:
            invalid.append(f"{prefix}的 fact_id 不在资料包可用事实中")
            continue
        submitted_fact = str(item.get("fact") or item.get("canonical_fact") or "").strip()
        canonical_text = str(canonical.get("canonical_fact") or canonical.get("fact") or "").strip()
        if _normalize(submitted_fact) != _normalize(canonical_text):
            invalid.append(f"{prefix}与 canonical_fact 不一致")
            continue
        ids = list(dict.fromkeys(str(value).strip() for value in item.get("source_ids") or item.get("supporting_source_ids") or [] if str(value).strip()))
        expected_ids = {str(value) for value in canonical.get("supporting_source_ids") or canonical.get("source_ids") or []}
        if not ids or any(source_id not in source_map for source_id in ids):
            invalid.append(f"{prefix}的 source_id 不存在或不是有效来源")
            continue
        if not set(ids).issubset(expected_ids):
            invalid.append(f"{prefix}引用了不支持该事实的来源")
            continue
        # Every cited source must pass independently. Never use any(source_supports_fact).
        unsupported = [source_id for source_id in ids if not _fact_supported(canonical_text, str(source_map[source_id].get("content") or source_map[source_id].get("summary") or ""))]
        if unsupported:
            invalid.append(f"{prefix}存在不支持该事实的引用来源：{', '.join(unsupported)}")
            continue
        if not _fact_supported(canonical_text, markdown):
            invalid.append(f"{prefix}没有在正文中实际使用")
            continue
        cited_publishers = {str(source_map[source_id].get("publisher_id") or source_map[source_id].get("domain") or source_id) for source_id in ids}
        is_official = str(canonical.get("verification_type") or "") == "official_single_source" or any(str(source_map[source_id].get("source_level") or "") == "official" or str(source_map[source_id].get("domain") or "").lower().endswith((".gov.cn", ".gov", ".edu.cn")) for source_id in ids)
        if not _single_source_attributed(markdown, canonical, source_map):
            invalid.append(f"{prefix}为单一来源信息，正文缺少“据XX报道/公开资料显示”等来源归属")
            continue
        validated += 1
        if not is_official and len(cited_publishers) >= 2:
            cross_verified += 1
    if duplicate_fact_ids:
        invalid.append(f"存在重复事实引用：{', '.join(sorted(duplicate_fact_ids))}")
    concrete_claims = _unsupported_concrete_claims(markdown, fact_map)
    if concrete_claims:
        invalid.append("正文存在未经 verified_facts 支持的具体陈述：" + "；".join(concrete_claims[:5]))
    total = len(unique_facts)
    return {
        "valid": not invalid,
        "validated_count": validated,
        "verified_fact_count": validated,
        "total_count": total,
        "source_coverage": round(validated / max(1, total), 4),
        "cross_verified_count": cross_verified,
        "duplicate_fact_ids": sorted(duplicate_fact_ids),
        "duplicate_fact_count": len(duplicate_fact_ids),
        "unsupported_concrete_claims": concrete_claims,
        "invalid_reasons": invalid[:20],
    }


def analyze_article(article: dict[str, Any], research_bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    bundle = research_bundle or {}
    markdown = str(article.get("content_markdown") or "")
    sentences = _sentences(markdown)
    facts = article.get("fact_basis") if isinstance(article.get("fact_basis"), list) else []
    sources = bundle.get("sources") if isinstance(bundle.get("sources"), list) else []
    publisher_ids = {str(item.get("publisher_id") or item.get("domain") or "") for item in sources if isinstance(item, dict) and item.get("fetch_success") and item.get("accepted_for_research") and not item.get("duplicate_of")}
    publisher_ids.discard("")
    source_count = len(publisher_ids)
    time_count = len(TIME_RE.findall(markdown))
    entity_text = " ".join(str(item) for item in bundle.get("key_people") or []) + " " + " ".join(str(item) for item in bundle.get("key_organizations") or [])
    entity_count = len({item for item in re.split(r"[,，、；;\s]+", entity_text) if len(item) >= 2})
    number_count = len(NUMBER_RE.findall(markdown))
    counts = Counter(_normalize(sentence) for sentence in sentences)
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    repetition_ratio = repeated / max(1, len(sentences))
    vague_count = sum(any(pattern in sentence for pattern in VAGUE_PATTERNS) for sentence in sentences)
    vague_ratio = vague_count / max(1, len(sentences))
    trace = validate_fact_basis(article, bundle) if facts else {"valid": False, "validated_count": 0, "verified_fact_count": 0, "total_count": 0, "source_coverage": 0.0, "cross_verified_count": 0, "duplicate_fact_ids": [], "duplicate_fact_count": 0, "unsupported_concrete_claims": _unsupported_concrete_claims(markdown, _canonical_fact_map(bundle)), "invalid_reasons": ["文章没有返回可追溯的 fact_basis"]}
    source_coverage = float(trace.get("source_coverage") or 0)
    target_word_count = int(article.get("word_count") or 0)
    actual_word_count = count_body_chinese_chars(article)
    article["body_char_count"] = actual_word_count
    length_ratio = actual_word_count / max(1, target_word_count) if target_word_count else 1.0
    score = min(100.0, round(
        min(30, int(trace.get("validated_count") or 0) * 6)
        + min(15, source_count * 5)
        + min(10, time_count * 5)
        + min(10, entity_count * 5)
        + min(10, number_count * 3)
        + source_coverage * 15
        + min(10, max(0.0, length_ratio) * 10)
        - repetition_ratio * 20
        - vague_ratio * 25,
        1,
    ))
    unique_basis_count = int(trace.get("total_count") or 0)
    intra = intra_article_quality(article, bundle)
    return {"word_count": actual_word_count, "target_word_count": target_word_count, "length_ratio": round(length_ratio, 4), "verified_fact_count": int(trace.get("validated_count") or 0), "fact_basis_count": unique_basis_count, "invalid_fact_count": max(0, unique_basis_count - int(trace.get("validated_count") or 0)), "cross_verified_fact_count": int(trace.get("cross_verified_count") or 0), "source_count": source_count, "publisher_count": source_count, "time_count": time_count, "entity_count": entity_count, "number_count": number_count, "repetition_score": round(repetition_ratio, 4), "vague_sentence_ratio": round(vague_ratio, 4), "source_coverage": round(source_coverage, 4), "sentence_count": len(sentences), "information_sufficiency_score": float(score), "fact_trace": trace, "intra_article_quality": intra}


def _cleanup_claim_text(text: str, claims: list[str]) -> str:
    cleaned = str(text or "").strip()
    claim_texts = [str(claim or "").strip() for claim in claims if str(claim or "").strip()]
    if not cleaned or not claim_texts:
        return cleaned
    sentences = re.split(r"(?<=[。！？!?；;])", cleaned)
    kept: list[str] = []
    for sentence in sentences:
        item = sentence.strip()
        if not item:
            continue
        if any(claim in item or item in claim for claim in claim_texts):
            continue
        kept.append(item)
    cleaned = "".join(kept) if kept else ""
    if not cleaned:
        return ""
    cleaned = re.sub(r"[，,、；;]\s*([。！？!?])", r"\1", cleaned)
    cleaned = re.sub(r"([，,、；;]){2,}", "，", cleaned)
    cleaned = re.sub(r"^[，,、；;。！？!?]+", "", cleaned)
    cleaned = re.sub(r"[，,、；;]+$", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" ，,、；;\n")


def _rebuild_markdown(article: dict[str, Any]) -> str:
    parts: list[str] = []
    title = str(article.get("title") or "").strip()
    intro = str(article.get("lead") or article.get("intro") or "").strip()
    if title:
        parts.append(f"# {title}")
    if intro:
        parts.append(intro)
    for section in article.get("sections") or []:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()
        body = str(section.get("body") or "").strip()
        if not body:
            continue
        parts.append(f"## {heading}\n{body}" if heading else body)
    return "\n\n".join(part for part in parts if part).strip()


def _rebuild_body_markdown(article: dict[str, Any]) -> str:
    parts: list[str] = []
    for section in article.get("sections") or []:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()
        body = str(section.get("body") or "").strip()
        if body:
            parts.append(f"## {heading}\n{body}" if heading else body)
    return "\n\n".join(part for part in parts if part).strip()


def sanitize_article_hard_facts(article: dict[str, Any], research_bundle: dict[str, Any] | None) -> dict[str, Any]:
    bundle = research_bundle or {}
    cleaned = dict(article)
    trace = validate_fact_basis(cleaned, bundle) if isinstance(cleaned.get("fact_basis"), list) and cleaned.get("fact_basis") else {
        "unsupported_concrete_claims": _unsupported_concrete_claims(str(cleaned.get("content_markdown") or ""), _canonical_fact_map(bundle)),
        "invalid_reasons": [],
    }
    claims = [
        str(item).strip()
        for item in trace.get("unsupported_concrete_claims") or []
        if str(item).strip() and HARD_FACT_WARNING_RE.search(str(item))
    ]
    if not claims:
        return {"article": cleaned, "removed_claims": [], "trace": trace}
    cleaned_intro = _cleanup_claim_text(str(cleaned.get("lead") or cleaned.get("intro") or ""), claims)
    cleaned["intro"] = cleaned_intro
    cleaned["lead"] = cleaned_intro
    if not cleaned_intro:
        cleaned["content_warning_code"] = str(cleaned.get("content_warning_code") or "LEAD_MISSING")
        cleaned["warning_note"] = str(cleaned.get("warning_note") or "导语经事实清洗后为空，请人工核对文章开头。")
    sections: list[dict[str, Any]] = []
    for section in cleaned.get("sections") or []:
        if not isinstance(section, dict):
            continue
        body = _cleanup_claim_text(str(section.get("body") or ""), claims)
        if body:
            sections.append({**section, "body": body})
    cleaned["sections"] = sections
    cleaned["body_markdown"] = _rebuild_body_markdown(cleaned)
    cleaned["content_markdown"] = _rebuild_markdown(cleaned)
    return {"article": cleaned, "removed_claims": claims, "trace": trace}


def _article_has_minimal_structure(article: dict[str, Any]) -> bool:
    """Return True when the article has enough structure and content to be exportable
    even if some quality checks flag hard errors (e.g. body slightly too short)."""
    intro = str(article.get("intro") or "").strip()
    sections = [s for s in (article.get("sections") or []) if isinstance(s, dict)]
    bodies = [str(s.get("body") or "").strip() for s in sections]
    intro_chars = len(re.findall(r"[\u4e00-\u9fff]", intro))
    has_intro = intro_chars >= 20
    nonempty_bodies = [b for b in bodies if re.findall(r"[\u4e00-\u9fff]", b)]
    has_sections = len(nonempty_bodies) >= 3
    body_chars = sum(len(re.findall(r"[\u4e00-\u9fff]", b)) for b in nonempty_bodies) + intro_chars
    return has_intro and has_sections and body_chars >= 400


def quality_gate(article: dict[str, Any], research_bundle: dict[str, Any] | None, *, minimum_score: float = 35.0) -> dict[str, Any]:
    metrics = analyze_article(article, research_bundle)
    bundle = research_bundle or {}
    hard_reasons: list[str] = []
    warning_reasons: list[str] = []
    warning_clauses = _soft_analysis_warning_clauses(str(article.get("content_markdown") or ""))
    markdown = str(article.get("content_markdown") or "")
    accepted_source_count = int(bundle.get("accepted_source_count") or 0)
    limited_research_mode = bool(bundle.get("hotlist_metadata_available") and str(bundle.get("research_status") or "") == "hotlist_limited")
    custom_topic_mode = bool(bundle.get("custom_topic") and str(bundle.get("research_status") or "") == "custom_topic")
    trace = metrics.get("fact_trace") or {}
    intra = metrics.get("intra_article_quality") or intra_article_quality(article, bundle)

    if not markdown.strip():
        hard_reasons.append("正文内容为空")
    if article.get("used_local_fallback") or article.get("fallback_kind"):
        hard_reasons.append("ARTICLE_TEXT_RETRY_REQUIRED")
    if not intra.get("passed", True):
        for code in intra.get("failures") or []:
            hard_reasons.append("SOURCE_CONTENT_CONTAMINATED" if code == "SOURCE_CONTENT_CONTAMINATED" else f"ARTICLE_QUALITY_BLOCKED:{code}")
    if accepted_source_count <= 0:
        if limited_research_mode:
            warning_reasons.append("当前仅获取到热榜标题和有限元数据，发布前请补充核对权威来源。")
        elif custom_topic_mode:
            warning_reasons.append("当前为用户手动输入话题，未使用公开资料来源；发布前请按需要补充案例、数据和参考链接。")
        else:
            hard_reasons.append("没有找到可用公开资料来源")

    if article.get("fact_basis") and not trace.get("valid", True):
        warning_reasons.extend(str(item) for item in (trace.get("invalid_reasons") or [])[:5])

    unsupported = [
        str(item).strip()
        for item in trace.get("unsupported_concrete_claims") or []
        if str(item).strip() and HARD_FACT_WARNING_RE.search(str(item))
    ]
    # hotlist_limited/custom_topic 模式下不过度拦截无来源支撑的claim
    if limited_research_mode or custom_topic_mode:
        unsupported = []
    hard_reasons.extend(f"正文具体陈述缺少来源资料支持：{claim[:80]}" for claim in unsupported[:5])

    if metrics["entity_count"] < 1 and accepted_source_count > 0:
        warning_reasons.append("正文中的人物或机构信息较少，建议人工复核表达完整度")

    format_warning = str(article.get("format_warning") or "").strip()
    if format_warning:
        warning_reasons.append(format_warning)

    # ── R1.2 新增质量检查 ──

    # 1. 模板腔检查
    template_phrases = ["从现有信息看", "值得关注", "引发关注", "仍需等待", "具有重要意义", "后续仍需"]
    template_hits: dict[str, int] = {}
    total_template_hits = 0
    for phrase in template_phrases:
        count = markdown.count(phrase)
        if count:
            template_hits[phrase] = count
            total_template_hits += count
    if any(count >= 3 for count in template_hits.values()) or total_template_hits >= 8:
        top_offenders = sorted(template_hits.items(), key=lambda x: -x[1])[:3]
        offender_desc = "；".join(f'"{phrase}" {count}次' for phrase, count in top_offenders)
        warning_reasons.append(f"模板套话偏多：{offender_desc}。建议替换为具体表述。")

    # 2. 正文长度检查（用 body_char_count 而非 markdown 全文字数）
    body_char_count = count_body_chinese_chars(article)
    article["body_char_count"] = body_char_count
    metrics["word_count"] = body_char_count
    target_word_count = int(metrics.get("target_word_count") or article.get("word_count") or 0)
    metrics["length_ratio"] = round(body_char_count / max(1, target_word_count), 4) if target_word_count else 1.0
    
    # ── R1.2.1 动态字数门槛：根据 word_count 区分 ──
    word_count = int(article.get("word_count") or bundle.get("word_count") or 1200)
    if word_count >= 1600:
        fail_threshold = 1400
        warn_threshold = 1599
        target_desc = "1400字"
    elif word_count >= 1500:
        fail_threshold = 1300
        warn_threshold = 1499
        target_desc = "1300字"
    else:
        fail_threshold = 1000
        warn_threshold = 1199
        target_desc = "1000字"
    
    if body_char_count < fail_threshold:
        hard_reasons.append(f"正文字数不足：{body_char_count} 字（最低要求 {target_desc}，目标 {word_count}字）")
    elif body_char_count <= warn_threshold:
        warning_reasons.append(f"正文字数偏低：{body_char_count} 字（目标 {word_count}字，建议 ≥ {word_count} 字）")

    # 3. 段落质量检查
    body_for_para = _article_body_for_fact_scan(markdown)
    natural_paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", body_for_para) if p.strip() and len(re.findall(r"[\u4e00-\u9fff]", p)) >= 30]
    if len(natural_paragraphs) < 4:
        warning_reasons.append(f"正文自然段不足：仅 {len(natural_paragraphs)} 段（建议 ≥ 4 段）")
    for i, para in enumerate(natural_paragraphs, 1):
        para_chars = len(re.findall(r"[\u4e00-\u9fff]", para))
        if para_chars > 260:
            warning_reasons.append(f"第 {i} 段过长：{para_chars} 字（建议 ≤ 260 字）")
    # 小节空正文检查
    for section in article.get("sections") or []:
        if not isinstance(section, dict):
            continue
        body = str(section.get("body") or "").strip()
        if not body or not re.findall(r"[\u4e00-\u9fff]", body):
            sec_heading = section.get("heading", "未命名")
            hard_reasons.append(f'小节"{sec_heading}"正文为空')

    # 4. 信息密度检查
    if accepted_source_count > 0:
        concrete_elements = re.findall(
            r"(?:20\d{2}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日|[A-Z]\w+(?:公司|集团|大学|医院|部门|委员会|协会|平台|组织|[省市县区]))",
            body_for_para,
        )
        if not concrete_elements:
            warning_reasons.append("正文缺少具体元素（机构名/时间/地点等），信息密度偏低")

    # ── R1.2.1 空话检测 ──
    unknown_phrases_found: list[str] = []
    total_unknown_chars = 0
    total_body_chars = len(re.findall(r"[\u4e00-\u9fff]", body_for_para))
    for phrase in UNKNOWN_PHRASES:
        count = body_for_para.count(phrase)  # 只在正文中检测，不含来源/AI声明
        if count:
            unknown_phrases_found.extend([phrase] * count)
            total_unknown_chars += len(phrase) * count
    if total_body_chars > 0:
        unknown_ratio = total_unknown_chars / total_body_chars
        if unknown_ratio > 0.08:
            hard_reasons.append(
                f"空话占比过高：{unknown_ratio:.0%}（'{unknown_phrases_found[0]}'等共{len(unknown_phrases_found)}处）。"
                f"信息核实困难时请转为'传播核验'或'读者判断路径'写法。"
            )
        elif unknown_ratio > 0.04:
            warning_reasons.append(f"空话偏多：{unknown_ratio:.0%}（'{unknown_phrases_found[0]}'等{len(unknown_phrases_found)}处），建议减少模糊表述")

    # ── R1.2.1 价值段落检测 ──
    value_hits: set[str] = set()
    for category, markers in VALUE_SECTION_MARKERS.items():
        if any(marker in body_for_para for marker in markers):
            value_hits.add(category)
    if len(value_hits) < 2:
        missing_categories = [cat for cat in VALUE_SECTION_MARKERS if cat not in value_hits]
        hard_reasons.append(
            f"有价值段落不足：仅检测到{len(value_hits)}类（{', '.join(sorted(value_hits)) if value_hits else '无'}）。"
            f"文章至少需要2类实质内容（核验路径/传播风险/背景解释/同类案例/读者启示/影响分析/明确观点）。"
            f"缺少：{', '.join(missing_categories[:3])}"
        )

    hard_reasons = list(dict.fromkeys(hard_reasons))
    warning_reasons = list(dict.fromkeys(warning_reasons))

    # ── R1.2 降级逻辑：文章结构完整、正文存在、无虚假硬事实 → warning ──
    if hard_reasons and _article_has_minimal_structure(article):
        degraded = [
            reason
            for reason in hard_reasons
            if reason.startswith("正文字数不足") or ("字数" in reason and "不足" in reason)
        ]
        still_hard = [reason for reason in hard_reasons if reason not in degraded]
        warning_reasons = list(dict.fromkeys(
            warning_reasons + degraded + [f"已降级（原文可导出）：{reason}" for reason in degraded]
        ))
        hard_reasons = still_hard

    status = "failed" if hard_reasons else "warning" if warning_reasons else "passed"
    return {
        "passed": status != "failed",
        "status": status,
        "draft_status": "完成但建议核对" if status == "warning" else "",
        "hard_error_count": len(hard_reasons),
        "warning_count": len(warning_reasons),
        "hard_errors": hard_reasons,
        "warnings": warning_reasons,
        "warning_clauses": warning_clauses,
        "reasons": hard_reasons + warning_reasons,
        "metrics": metrics,
    }
