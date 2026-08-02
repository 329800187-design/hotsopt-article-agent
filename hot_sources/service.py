from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import time
from typing import Any

import httpx

from hot_sources.cache import LocalCacheProvider
from hot_sources.commercial_filter import blocked_topic_reason, filter_public_hotspots
from hot_sources.dedupe import deduplicate_topics
from hot_sources.manual import ManualHotSource
from hot_sources.newsnow import NewsNowSource
from hot_sources.registry import get_daily_source
from hot_sources.toutiao_official import ToutiaoOfficialSource
from hot_sources.tophub import TopHubOverviewSource, TopHubToutiaoSource
from modules.database import DB_PATH, SQLiteStore, get_store
from modules.models import HotTopic, utc_now
from modules.topic_cache import TopicCacheStore, get_default_cache_store
from modules.network import classify_network_error
from modules.security import redact_sensitive_text, sanitize_sensitive_data


class HotTrendService:
    def __init__(self, settings: dict[str, Any] | None = None, store: SQLiteStore | None = None, providers: list[Any] | None = None, cache_provider: Any | None = None, cache_store: TopicCacheStore | None = None) -> None:
        if not settings:
            from modules.config_store import load_settings
            settings = load_settings()
        self.settings = settings
        self.store = store or get_store()
        if cache_store is not None:
            self.cache_store = cache_store
        elif self.store.db_path.resolve() == DB_PATH.resolve():
            self.cache_store = get_default_cache_store()
        else:
            self.cache_store = TopicCacheStore(self.store.db_path.parent / "cache" / "latest_topics.json", environment="test")
        network_settings = settings.get("network", {})
        if providers is not None:
            self.providers = providers
        else:
            self.providers = [ToutiaoOfficialSource(network_settings=network_settings)]
            daily_url = str(settings.get("hot_source_url") or "").strip()
            if daily_url:
                self.providers.append(get_daily_source(daily_url, network_settings=network_settings))
            self.providers.extend([
                NewsNowSource(network_settings=network_settings),
                TopHubOverviewSource(network_settings=network_settings),
                TopHubToutiaoSource(network_settings=network_settings),
                TopHubToutiaoSource(network_settings=network_settings, endpoint="https://tophub.today/n/KqndgxeLl9", provider_name="tophub_weibo", display_name="今日热榜 微博"),
                TopHubToutiaoSource(network_settings=network_settings, endpoint="https://tophub.today/n/mproPpoq6O", provider_name="tophub_zhihu", display_name="今日热榜 知乎"),
                TopHubToutiaoSource(network_settings=network_settings, endpoint="https://tophub.today/n/Jb0vmloB1G", provider_name="tophub_baidu", display_name="今日热榜 百度"),
                TopHubToutiaoSource(network_settings=network_settings, endpoint="https://tophub.today/n/74KvxwokxM", provider_name="tophub_bilibili", display_name="今日热榜 哔哩哔哩"),
                TopHubToutiaoSource(network_settings=network_settings, endpoint="https://tophub.today/n/DpQvNABoNE", provider_name="tophub_douyin", display_name="今日热榜 抖音"),
            ])
        self.cache_provider = cache_provider if cache_provider is not None else LocalCacheProvider(self.cache_store)

    @staticmethod
    def _safe_topics(topics: list[HotTopic]) -> list[HotTopic]:
        return [HotTopic.from_dict(sanitize_sensitive_data(topic.to_dict())) for topic in topics]

    @staticmethod
    def _filter_stats(raw_count: int, deduped_count: int, valid_count: int, filtered: list[dict[str, Any]], duplicate_count: int = 0, invalid_count: int = 0) -> dict[str, Any]:
        return {
            "raw_count": raw_count,
            "valid_hotspot_count": valid_count,
            "filtered_commercial_count": len(filtered),
            "filtered_duplicate_count": duplicate_count,
            "filtered_invalid_count": invalid_count,
            "final_display_count": valid_count,
            "deduplicated_count": deduped_count,
            "filtered_commercial_examples": filtered[:20],
        }

    @staticmethod
    def _hotlist_evidence(topics: list[HotTopic], *, provider_name: str, status: str, captured_at: str, cache_age_seconds: float | None = None) -> dict[str, Any]:
        return {
            "captured_at": captured_at,
            "topic_count": len(topics),
            "source_kind": "primary" if status == "online" and provider_name == "toutiao_official" else "fallback" if status == "online" else "cache",
            "provider_name": provider_name,
            "cache_age_seconds": cache_age_seconds,
            "topics": [{"rank": item.rank, "title": item.title, "hot_value": item.hot_value, "category": item.category, "source_name": item.source_name, "captured_at": item.captured_at, "source_url": item.source_url} for item in topics[:10]],
        }

    @staticmethod
    def _failure_code(exc: Exception) -> str:
        if isinstance(exc, httpx.TimeoutException):
            return "TIMEOUT"
        if isinstance(exc, httpx.HTTPStatusError):
            return {
                403: "HTTP_403",
                404: "HTTP_404",
                429: "HTTP_429",
            }.get(exc.response.status_code, f"HTTP_{exc.response.status_code}")
        text = str(exc).lower()
        if "expecting value" in text or "non-json" in text:
            return "NON_JSON_RESPONSE"
        if "没有识别" in str(exc):
            return "HTML_STRUCTURE_CHANGED"
        if "empty" in text or "为空" in str(exc):
            return "EMPTY_RESULT"
        if "dns" in text or "name or service not known" in text:
            return "DNS_ERROR"
        if "ssl" in text or "tls" in text or "certificate" in text:
            return "TLS_ERROR"
        return "PARSE_ERROR"

    def refresh(self) -> dict[str, Any]:
        errors: list[str] = []
        merged_topics: list[HotTopic] = []
        successful_providers: list[tuple[str, str, str]] = []
        provider_diagnostics: list[dict[str, Any]] = []
        refresh_started = time.perf_counter()
        for provider in self.providers:
            provider_started = time.perf_counter()
            request_url = str(getattr(provider, "url", "") or getattr(provider, "endpoint", "") or "")
            try:
                fetched_topics = provider.fetch_trends()
                topics = deduplicate_topics(fetched_topics)
                for topic in topics:
                    topic.provider_status = "online"
                    topic.is_cached = False
                    topic.source_name = provider.display_name
                    topic.updated_at = utc_now()
                self.store.save_provider_status(provider.provider_name, provider.display_name, "online", provider.last_success_at, None)
                merged_topics.extend(topics)
                successful_providers.append((provider.provider_name, provider.display_name, provider.last_success_at or utc_now()))
                provider_diagnostics.append({
                    "provider_name": provider.provider_name,
                    "request_url": request_url,
                    "http_status": int(getattr(provider, "last_http_status", 200) or 200),
                    "content_type": str(getattr(provider, "last_content_type", "") or ""),
                    "raw_item_count": int(getattr(provider, "last_raw_item_count", len(fetched_topics)) or 0),
                    "normalized_item_count": len(fetched_topics),
                    "deduplicated_item_count": len(topics),
                    "elapsed_ms": int((time.perf_counter() - provider_started) * 1000),
                    "failure_code": "",
                    "failure_message": "",
                    "captured_at": provider.last_success_at or utc_now(),
                })
            except Exception as exc:
                detail = classify_network_error(exc)
                provider.last_error = detail["message"]
                safe_display_name = redact_sensitive_text(provider.display_name)
                errors.append(f"{safe_display_name} [{detail['category']}]: {detail['message']}")
                self.store.save_provider_status(provider.provider_name, provider.display_name, "error", provider.last_success_at, detail["message"])
                response = getattr(exc, "response", None)
                provider_diagnostics.append({
                    "provider_name": provider.provider_name,
                    "request_url": request_url,
                    "http_status": int(getattr(response, "status_code", 0) or 0),
                    "content_type": str(getattr(response, "headers", {}).get("content-type") or ""),
                    "raw_item_count": 0,
                    "normalized_item_count": 0,
                    "deduplicated_item_count": 0,
                    "elapsed_ms": int((time.perf_counter() - provider_started) * 1000),
                    "failure_code": self._failure_code(exc),
                    "failure_message": redact_sensitive_text(str(exc))[:500],
                    "captured_at": utc_now(),
                })
        if merged_topics:
            pre_dedupe_total = len(merged_topics)
            deduped_topics = deduplicate_topics(merged_topics)
            topics, filtered_commercial = filter_public_hotspots(deduped_topics)
            topics = topics[:200]
            captured_at = max((topic.captured_at for topic in topics if topic.captured_at), default=utc_now())
            primary_name, primary_display, _ = successful_providers[0]
            display_name = " + ".join(dict.fromkeys(display for _, display, _ in successful_providers))
            self.store.save_topics(topics, record_observation=True)
            self.cache_store.save(topics, display_name)
            safe_topics = self._safe_topics(topics)
            evidence = self._hotlist_evidence(safe_topics, provider_name=primary_name, status="online", captured_at=captured_at)
            evidence["provider_names"] = [name for name, _, _ in successful_providers]
            evidence["provider_count"] = len(successful_providers)
            filter_stats = self._filter_stats(pre_dedupe_total, len(deduped_topics), len(safe_topics), filtered_commercial, duplicate_count=max(0, pre_dedupe_total - len(deduped_topics)))
            evidence["filter_stats"] = filter_stats
            return {
                "topics": safe_topics,
                "provider_name": primary_name,
                "display_name": display_name,
                "status": "online",
                "is_cached": False,
                "stale": False,
                "captured_at": captured_at,
                "errors": errors,
                "last_error": "",
                "hotlist_evidence": evidence,
                "provider_diagnostics": provider_diagnostics,
                "filter_stats": filter_stats,
                "pre_dedupe_live_topics": pre_dedupe_total,
                "deduplicated_live_topics": len(safe_topics),
                "topics_with_url_or_identifier": sum(bool(item.source_url or item.source) for item in safe_topics),
                "topics_with_captured_at": sum(bool(item.captured_at) for item in safe_topics),
                "cached_topic_count": 0,
                "elapsed_ms": int((time.perf_counter() - refresh_started) * 1000),
            }
        try:
            cached_raw = self.cache_provider.fetch_trends()
            deduped_cached = deduplicate_topics(cached_raw)
            topics, filtered_commercial = filter_public_hotspots(deduped_cached)
            self.store.save_topics(topics, record_observation=False)
            safe_topics = self._safe_topics(topics)
            age = self.cache_age_seconds()
            stale = age is not None and age > self.cache_ttl_seconds()
            warning = "本地缓存已过期" if stale else ""
            self.store.save_provider_status(self.cache_provider.provider_name, self.cache_provider.display_name, "online" if not stale else "stale", self.cache_provider.last_success_at, warning)
            captured_at = safe_topics[0].captured_at if safe_topics else utc_now()
            filter_stats = self._filter_stats(len(cached_raw), len(deduped_cached), len(safe_topics), filtered_commercial, duplicate_count=max(0, len(cached_raw) - len(deduped_cached)))
            hotlist_evidence = self._hotlist_evidence(safe_topics, provider_name=self.cache_provider.provider_name, status="cached", captured_at=captured_at, cache_age_seconds=age)
            hotlist_evidence["filter_stats"] = filter_stats
            return {"topics": safe_topics, "provider_name": self.cache_provider.provider_name, "display_name": self.cache_provider.display_name, "status": "cached", "is_cached": True, "stale": stale, "captured_at": captured_at, "errors": errors, "last_error": "；".join(errors + ([warning] if warning else [])), "hotlist_evidence": hotlist_evidence, "filter_stats": filter_stats, "provider_diagnostics": provider_diagnostics, "pre_dedupe_live_topics": 0, "deduplicated_live_topics": 0, "topics_with_url_or_identifier": 0, "topics_with_captured_at": 0, "cached_topic_count": len(safe_topics), "cache_age_seconds": age, "elapsed_ms": int((time.perf_counter() - refresh_started) * 1000)}
        except Exception as exc:
            detail = classify_network_error(exc)
            safe_display_name = redact_sensitive_text(self.cache_provider.display_name)
            cache_error = f"{safe_display_name} [{detail['category']}]: {detail['message']}"
            errors.append(cache_error)
            self.store.save_provider_status(self.cache_provider.provider_name, self.cache_provider.display_name, "error", self.cache_provider.last_success_at, detail["message"])
            return {"topics": [], "provider_name": "none", "display_name": "无可用来源", "status": "offline", "is_cached": False, "stale": True, "captured_at": "", "errors": errors, "last_error": "；".join(errors), "hotlist_evidence": {"captured_at": "", "topic_count": 0, "source_kind": "offline", "provider_name": "none", "cache_age_seconds": None, "topics": []}}

    def cache_age_seconds(self) -> float | None:
        return self.cache_store.get_age_seconds()

    def cache_ttl_seconds(self) -> int:
        return max(60, int(self.settings.get("hot_cache_ttl_seconds", 6 * 60 * 60)))

    def list_topics(self, keyword: str = "", category: str = "全部", source: str = "全部", sort: str = "captured_at_desc", time_range: str = "全部时间") -> list[HotTopic]:
        captured_after = None
        if time_range != "全部时间":
            hours = {"最近1小时": 1, "最近6小时": 6, "最近24小时": 24}.get(time_range)
            if hours:
                captured_after = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        topics = self.store.list_topics(keyword, category, source, sort, captured_after)
        valid, _ = filter_public_hotspots(topics)
        return valid

    def add_manual_topic(self, title: str, summary: str = "", source_url: str = "") -> HotTopic:
        topics = ManualHotSource(title, summary, source_url=source_url).fetch()
        if not topics:
            raise ValueError("自定义话题不能为空")
        topic = topics[0]
        self.store.save_topics([topic], record_observation=True)
        return self._safe_topics([topic])[0]

    def select_topics(self, topic_ids: list[str]) -> list[HotTopic]:
        if not 1 <= len(topic_ids) <= 5:
            raise ValueError("一次最多选择 5 个话题，且至少选择 1 个")
        all_topics = {topic.id: topic for topic in self.store.list_topics(limit=500)}
        selected = [all_topics[topic_id] for topic_id in topic_ids if topic_id in all_topics]
        if len(selected) != len(topic_ids):
            raise ValueError("存在无效或已不存在的话题")
        blocked = [(topic, blocked_topic_reason(topic)) for topic in selected]
        blocked = [(topic, reason) for topic, reason in blocked if reason]
        if blocked:
            topic, reason = blocked[0]
            details = {
                "hotspot_id": topic.id,
                "title": topic.title,
                "commercial_score": reason.get("commercial_score", 0),
                "matched_signals": reason.get("matched_signals", []),
                "filter_reason": reason.get("filter_reason"),
            }
            raise ValueError("TOPIC-COMMERCIAL-FILTERED:" + json.dumps(details, ensure_ascii=False))
        return selected

    def get_basket(self) -> list[dict[str, Any]]:
        return self.store.get_basket()

    def add_to_basket(self, topic_ids: list[str]) -> list[dict[str, Any]]:
        if len(set(str(topic_id) for topic_id in topic_ids)) != len(topic_ids):
            raise ValueError("TOPIC-SELECT-DUPLICATE")
        selected = self.select_topics(topic_ids)
        current = self.store.get_basket()
        current_ids = {str(item.get("id")) for item in current}
        additions = [topic.to_dict() for topic in selected if topic.id not in current_ids]
        if not additions:
            raise ValueError("TOPIC-SELECT-DUPLICATE")
        if len(current) + len(additions) > 5:
            raise ValueError("TOPIC-SELECT-LIMIT")
        merged = current + additions
        return self.store.set_basket(merged)

    def remove_from_basket(self, topic_id: str) -> list[dict[str, Any]]:
        current = self.store.get_basket()
        next_items = [item for item in current if item.get("id") != topic_id]
        if len(next_items) == len(current):
            raise ValueError("TOPIC-REMOVE-FAILED")
        return self.store.set_basket(next_items)

    def clear_basket(self) -> list[dict[str, Any]]:
        return self.store.set_basket([])

    def reorder_basket(self, topic_ids: list[str]) -> list[dict[str, Any]]:
        current = {str(item.get("id")): item for item in self.store.get_basket()}
        if set(topic_ids) != set(current) or len(topic_ids) > 5:
            raise ValueError("选题篮排序列表必须包含当前全部话题")
        return self.store.set_basket([current[topic_id] for topic_id in topic_ids])

    def create_task(self, task_name: str, mode: str, topic_ids: list[str], article_count: int, generation_options: dict[str, Any] | None = None) -> dict[str, Any]:
        topics = self.select_topics(topic_ids)
        if mode == "multi_topic" and article_count > len(topics):
            raise ValueError("多热点模式的文章数量不能超过已选择的话题数量")
        if mode == "single_topic_multi_angle" and len(topics) != 1:
            raise ValueError("单热点五角度模式只能选择一个话题")
        return self.store.create_task(task_name, mode, [topic.to_dict() for topic in topics], article_count, generation_options=generation_options)
