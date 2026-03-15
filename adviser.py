#!/usr/bin/env python3
"""使用 AIHUBMIX + DuckDuckGo 生成股票投资建议（短线/长线）。"""

from __future__ import annotations

import argparse
import builtins
from contextlib import nullcontext
import sys
import html
import json
import math
import os
import random
import re
import smtplib
import time
import textwrap
from urllib.parse import quote_plus, urlparse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Iterable, List
from zoneinfo import ZoneInfo


_TRADE_DATE_CACHE: dict[str, object] = {"date": None, "value": None}
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def request_with_retry(request_callable, *args, retries: int = 3, backoff_seconds: float = 1.0, **kwargs):
    import requests

    for attempt in range(1, retries + 1):
        try:
            return request_callable(*args, **kwargs)
        except requests.Timeout:
            if attempt == retries:
                raise
            time.sleep(backoff_seconds * (2 ** (attempt - 1)))
        except requests.RequestException:
            raise




def configure_realtime_stdout() -> None:
    """尽量启用行缓冲，确保运行进度实时输出。"""
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(line_buffering=True, write_through=True)


def realtime_print(*args, **kwargs) -> None:
    """统一使用 flush 输出，避免日志堆积到结束才打印。"""
    if "flush" not in kwargs:
        kwargs["flush"] = True
    builtins.print(*args, **kwargs)


@dataclass
class Config:
    llm_provider: str
    aihubmix_api_key: str
    aihubmix_base_url: str
    aihubmix_model: str
    stock_codes: List[str]
    max_search_results: int
    search_region: str
    nim_api_key: str = ""
    nim_base_url: str = "https://integrate.api.nvidia.com/v1"
    nim_model: str = "meta/llama-3.1-70b-instruct"
    email_stock_router: dict[str, List[str]] = field(default_factory=dict)
    sender_email: str | None = None
    sender_auth_code: str | None = None
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: str = "ssl"
    email_delivery_protocol: str = "smtp"
    exchange_tenant_id: str | None = None
    exchange_client_id: str | None = None
    exchange_client_secret: str | None = None
    exchange_sender_upn: str | None = None
    market_data_provider: str = "auto"
    chain_of_search_depth: int = 1
    search_reflection_max_rounds: int = 3


@dataclass
class StockResearchResult:
    stock_code: str
    stock_name: str | None
    contexts: List[dict]
    advice: str
    brief_summary: str


def load_config() -> Config:
    llm_provider = (os.getenv("LLM_PROVIDER", "aihubmix").strip().lower() or "aihubmix")
    if llm_provider not in {"aihubmix", "nim"}:
        raise ValueError("环境变量 LLM_PROVIDER 仅支持 aihubmix / nim")

    api_key = os.getenv("AIHUBMIX_API_KEY", "").strip()
    base_url = normalize_base_url(
        os.getenv("AIHUBMIX_BASE_URL", "https://api.aihubmix.com/v1"),
        var_name="AIHUBMIX_BASE_URL",
    )
    model = os.getenv("AIHUBMIX_MODEL", "gpt-4o-mini").strip()

    nim_api_key = (os.getenv("NVIDIA_NIM_API_KEY", "").strip() or os.getenv("NIM_API_KEY", "").strip())
    nim_base_url = normalize_base_url(
        os.getenv("NVIDIA_NIM_BASE_URL", os.getenv("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")),
        var_name="NVIDIA_NIM_BASE_URL",
        fallback_url="https://integrate.api.nvidia.com/v1",
    )
    nim_model = (
        os.getenv("NVIDIA_NIM_MODEL", "").strip()
        or os.getenv("NIM_MODEL", "").strip()
        or "meta/llama-3.1-70b-instruct"
    )

    if llm_provider == "aihubmix" and not api_key:
        raise ValueError("缺少环境变量 AIHUBMIX_API_KEY")
    if llm_provider == "nim" and not nim_api_key:
        raise ValueError("缺少环境变量 NVIDIA_NIM_API_KEY（或 NIM_API_KEY）")

    router = parse_email_stock_router(os.getenv("EMAIL_STOCK_ROUTER", ""))

    raw_codes = os.getenv("STOCK_CODES", "").strip()
    stock_codes = [code.strip() for code in raw_codes.split(",") if code.strip()]
    if not stock_codes:
        stock_codes = sorted({code for codes in router.values() for code in codes})

    if not stock_codes:
        raise ValueError(
            "缺少股票配置：请设置 STOCK_CODES 或 EMAIL_STOCK_ROUTER（例如 a@x.com:AAPL,TSLA;b@y.com:MSFT）"
        )

    raw_max_results = os.getenv("DUCKDUCKGO_MAX_RESULTS", "5").strip()
    if not raw_max_results:
        raw_max_results = "5"

    try:
        max_search_results = int(raw_max_results)
    except ValueError as exc:
        raise ValueError("环境变量 DUCKDUCKGO_MAX_RESULTS 必须是整数") from exc

    if max_search_results <= 0:
        raise ValueError("环境变量 DUCKDUCKGO_MAX_RESULTS 必须大于 0")

    search_region = os.getenv("DUCKDUCKGO_REGION", "zh-cn").strip() or "zh-cn"

    sender_email = os.getenv("SENDER_EMAIL", "").strip() or None
    sender_auth_code = os.getenv("SENDER_AUTH_CODE", "").strip() or None
    smtp_host = os.getenv("SMTP_HOST", "").strip() or infer_smtp_host(sender_email)
    raw_smtp_port = os.getenv("SMTP_PORT", "465").strip() or "465"
    try:
        smtp_port = int(raw_smtp_port)
    except ValueError as exc:
        raise ValueError("环境变量 SMTP_PORT 必须是整数") from exc

    smtp_security = os.getenv("SMTP_SECURITY", "ssl").strip().lower() or "ssl"
    if smtp_security not in {"ssl", "starttls", "plain"}:
        raise ValueError("环境变量 SMTP_SECURITY 仅支持 ssl / starttls / plain")

    email_delivery_protocol = os.getenv("EMAIL_DELIVERY_PROTOCOL", "smtp").strip().lower() or "smtp"
    supported_protocols = {"smtp", "pop3", "imap", "exchange", "carddav"}
    if email_delivery_protocol not in supported_protocols:
        raise ValueError("环境变量 EMAIL_DELIVERY_PROTOCOL 仅支持 smtp / pop3 / imap / exchange / carddav")

    chain_of_search_depth = safe_int(os.getenv("CHAIN_OF_SEARCH_DEPTH", "1"), default=1)
    if chain_of_search_depth <= 0:
        raise ValueError("环境变量 CHAIN_OF_SEARCH_DEPTH 必须大于 0")

    search_reflection_max_rounds = safe_int(os.getenv("SEARCH_REFLECTION_MAX_ROUNDS", "3"), default=3)
    if search_reflection_max_rounds <= 0:
        raise ValueError("环境变量 SEARCH_REFLECTION_MAX_ROUNDS 必须大于 0")

    market_data_provider = os.getenv("MARKET_DATA_PROVIDER", "auto").strip().lower() or "auto"
    if market_data_provider not in {"auto", "akshare", "yahoo", "eastmoney", "sina", "tencent", "stooq"}:
        raise ValueError(
            "环境变量 MARKET_DATA_PROVIDER 仅支持 auto / akshare / yahoo / eastmoney / sina / tencent / stooq"
        )

    return Config(
        llm_provider=llm_provider,
        aihubmix_api_key=api_key,
        aihubmix_base_url=base_url.rstrip("/"),
        aihubmix_model=model,
        nim_api_key=nim_api_key,
        nim_base_url=nim_base_url.rstrip("/"),
        nim_model=nim_model,
        stock_codes=stock_codes,
        max_search_results=max_search_results,
        search_region=search_region,
        email_stock_router=router,
        sender_email=sender_email,
        sender_auth_code=sender_auth_code,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_security=smtp_security,
        email_delivery_protocol=email_delivery_protocol,
        exchange_tenant_id=os.getenv("EXCHANGE_TENANT_ID", "").strip() or None,
        exchange_client_id=os.getenv("EXCHANGE_CLIENT_ID", "").strip() or None,
        exchange_client_secret=os.getenv("EXCHANGE_CLIENT_SECRET", "").strip() or None,
        exchange_sender_upn=os.getenv("EXCHANGE_SENDER_UPN", "").strip() or sender_email,
        market_data_provider=market_data_provider,
        chain_of_search_depth=chain_of_search_depth,
        search_reflection_max_rounds=search_reflection_max_rounds,
    )


def parse_email_stock_router(raw_router: str) -> dict[str, List[str]]:
    """解析 EMAIL_STOCK_ROUTER（支持分号或换行分隔）。"""
    router: dict[str, List[str]] = {}
    value = raw_router.strip()
    if not value:
        return router

    items = re.split(r"[;\n\r]+", value)
    for item in items:
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(
                "EMAIL_STOCK_ROUTER 格式错误，示例：a@x.com:AAPL,TSLA\\nb@y.com:MSFT"
            )
        email, stocks_blob = item.split(":", 1)
        receiver = email.strip()
        stocks = [code.strip().upper() for code in stocks_blob.split(",") if code.strip()]
        if not receiver or not stocks:
            raise ValueError(
                "EMAIL_STOCK_ROUTER 格式错误，示例：a@x.com:AAPL,TSLA\\nb@y.com:MSFT"
            )
        deduped = list(dict.fromkeys(stocks))
        router[receiver] = deduped

    return router


def infer_smtp_host(sender_email: str | None) -> str:
    if not sender_email or "@" not in sender_email:
        return ""

    domain = sender_email.split("@", 1)[1].lower()
    mapping = {
        "qq.com": "smtp.qq.com",
        "163.com": "smtp.163.com",
        "126.com": "smtp.126.com",
        "gmail.com": "smtp.gmail.com",
        "outlook.com": "smtp.office365.com",
        "hotmail.com": "smtp.office365.com",
    }
    return mapping.get(domain, f"smtp.{domain}")


def normalize_base_url(
    raw_base_url: str,
    var_name: str = "AIHUBMIX_BASE_URL",
    fallback_url: str = "https://api.aihubmix.com/v1",
) -> str:
    base_url = raw_base_url.strip()
    if not base_url:
        return fallback_url.rstrip("/")

    parsed = urlparse(base_url)

    if parsed.scheme and parsed.netloc:
        return base_url.rstrip("/")

    if not parsed.scheme and parsed.path and not parsed.path.startswith("/"):
        with_scheme = f"https://{parsed.path}"
        parsed_with_scheme = urlparse(with_scheme)
        if parsed_with_scheme.netloc:
            return with_scheme.rstrip("/")

    raise ValueError(
        f"环境变量 {var_name} 无效，请提供完整 URL（例如 https://api.aihubmix.com/v1）"
    )


def active_llm_api_key(config: Config) -> str:
    if config.llm_provider == "nim":
        return config.nim_api_key
    return config.aihubmix_api_key


def active_llm_base_url(config: Config) -> str:
    if config.llm_provider == "nim":
        return config.nim_base_url
    return config.aihubmix_base_url


def active_llm_model(config: Config) -> str:
    if config.llm_provider == "nim":
        return config.nim_model
    return config.aihubmix_model


def active_llm_label(config: Config) -> str:
    if config.llm_provider == "nim":
        return "NVIDIA NIM"
    return "AIHUBMIX"


def build_queries(stock_code: str, adaptive_topics: list[str] | None = None) -> List[str]:
    aliases = stock_code_aliases(stock_code)
    topic_templates = [
        "{alias} 股票 最新消息",
        "{alias} 财报 业绩 指引",
        "{alias} 股价 分析 技术指标",
        "{alias} 海外新闻 宏观经济 影响",
        "{alias} 全球市场 利率 通胀 地缘政治 风险",
    ]

    if adaptive_topics:
        deduped_topics = list(dict.fromkeys(topic.strip() for topic in adaptive_topics if topic.strip()))
        for topic in deduped_topics[:3]:
            topic_templates.append(f"{{alias}} {topic} 行业政策 影响")

    queries: List[str] = []
    seen_query_keys: set[str] = set()
    for alias in aliases:
        for template in topic_templates:
            query = template.format(alias=alias)
            key = normalize_query_key(query)
            if key in seen_query_keys:
                continue
            seen_query_keys.add(key)
            queries.append(query)

    return queries


def stock_code_aliases(stock_code: str) -> List[str]:
    """给同一股票代码构造多个常见别名，提升检索命中率。"""
    code = stock_code.strip().upper()
    if not code:
        return []

    aliases = [code]

    if code.isdigit() and len(code) == 6:
        if code.startswith("6"):
            aliases.append(f"{code}.SH")
            aliases.append(f"上证{code}")
        else:
            aliases.append(f"{code}.SZ")
            aliases.append(f"深证{code}")

    return list(dict.fromkeys(aliases))


def normalize_query_key(query: str) -> str:
    """对 query 做语义归一化，避免 600900 与 sh600900 这类重复检索。"""
    text = query.upper().strip()
    replacements = {
        "SH": "",
        "SZ": "",
        ".SH": "",
        ".SZ": "",
        "上证": "",
        "深证": "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text)


def search_with_retry(ddgs, query: str, region: str, max_results: int, max_retries: int = 3) -> List[dict]:
    for attempt in range(1, max_retries + 1):
        try:
            # 用随机小延迟降低触发搜索限流的概率。
            time.sleep(random.uniform(0.8, 1.6))
            return list(
                ddgs.text(
                    query,
                    region=region,
                    max_results=max_results,
                    safesearch="off",
                )
            )
        except Exception as exc:
            if attempt == max_retries:
                realtime_print(f"[进度] 检索异常（query={query}, region={region}）：{exc}")
                return []
            time.sleep(2 ** (attempt - 1))

    return []


def search_context(stock_code: str, max_results: int, region: str) -> List[dict]:
    queries = build_queries(stock_code)
    realtime_print(f"[进度] {stock_code}: 开始检索，共 {len(queries)} 条查询")
    results = search_context_via_queries(stock_code, queries, max_results, region)
    realtime_print(f"[进度] {stock_code}: 检索完成，共收集 {len(results)} 条")
    return results


def search_context_chain(stock_code: str, max_results: int, region: str, depth: int) -> List[dict]:
    """多轮检索：上一轮热点词会驱动下一轮 query，实现 chain of search。"""
    combined: list[dict] = []
    seen_urls: set[str] = set()
    frontier_topics: list[str] = []

    for round_idx in range(1, depth + 1):
        round_queries = build_queries(stock_code, adaptive_topics=frontier_topics)

        realtime_print(f"[进度] {stock_code}: chain-of-search 第 {round_idx}/{depth} 轮，query 数={len(round_queries)}")
        round_results = search_context_via_queries(stock_code, round_queries, max_results, region)
        for hit in round_results:
            href = hit.get("href", "")
            if href and href in seen_urls:
                continue
            if href:
                seen_urls.add(href)
            combined.append(hit)

        frontier_topics = extract_followup_topics(round_results, stock_code)
        if not frontier_topics:
            break

    return combined


def search_context_via_queries(stock_code: str, queries: list[str], max_results: int, region: str) -> list[dict]:
    """对外暴露的 search_context 的可复用底层。"""
    from ddgs import DDGS

    results: list[dict] = []
    fallback_regions = list(dict.fromkeys([region or "zh-cn", "zh-cn", "wt-wt"]))
    seen_urls = set()
    with temporary_search_proxy_env(stock_code):
        for current_region in fallback_regions:
            with DDGS() as ddgs:
                for idx, query in enumerate(queries, start=1):
                    realtime_print(
                        f"[进度] {stock_code}: 检索 {idx}/{len(queries)} -> {query}"
                        f"（region={current_region}）"
                    )
                    hits: Iterable[dict] = search_with_retry(ddgs, query, current_region, max_results)
                    query_count = 0
                    for hit in hits:
                        published_at = parse_published_at(hit)
                        if not published_at or not within_last_3_months(published_at):
                            continue
                        href = hit.get("href", "")
                        if href and href in seen_urls:
                            continue
                        if href:
                            seen_urls.add(href)
                        query_count += 1
                        results.append(
                            {
                                "query": query,
                                "region": current_region,
                                "title": hit.get("title", ""),
                                "href": href,
                                "body": hit.get("body", ""),
                                "published_at": published_at.date().isoformat(),
                            }
                        )
                    realtime_print(f"[进度] {stock_code}: 该查询命中 {query_count} 条")
            if results:
                break

    if len(results) < max_results:
        rss_results = search_context_via_rss(stock_code, queries, max_results=max_results * 2)
        for hit in rss_results:
            href = hit.get("href", "")
            if href and href in seen_urls:
                continue
            if href:
                seen_urls.add(href)
            results.append(hit)

    return results


def merge_context_hits(existing: list[dict], new_hits: list[dict]) -> list[dict]:
    merged = list(existing)
    seen_keys = {
        (row.get("href", "").strip(), row.get("title", "").strip())
        for row in existing
    }
    for hit in new_hits:
        key = (hit.get("href", "").strip(), hit.get("title", "").strip())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        merged.append(hit)
    return merged


def parse_json_object_from_text(text: str) -> dict:
    cleaned = text.strip()
    fenced_match = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", cleaned, flags=re.IGNORECASE)
    if fenced_match:
        cleaned = fenced_match.group(1)
    else:
        obj_match = re.search(r"(\{[\s\S]*\})", cleaned)
        if obj_match:
            cleaned = obj_match.group(1)
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("模型返回不是 JSON 对象")
    return parsed


def assess_information_sufficiency(
    config: Config,
    stock_code: str,
    contexts: list[dict],
    model_name: str,
) -> tuple[bool, list[str], str]:
    import requests

    url = f"{active_llm_base_url(config)}/chat/completions"
    headers = {
        "Authorization": f"Bearer {active_llm_api_key(config)}",
        "Content-Type": "application/json",
    }
    latest_contexts = contexts[-20:]
    context_lines = [
        f"[{i}] date={item.get('published_at', 'unknown')} title={item.get('title', '')} summary={item.get('body', '')}"
        for i, item in enumerate(latest_contexts, start=1)
    ]
    payload = {
        "model": model_name,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是投研检索审计员。请评估当前信息是否足以支撑高质量的交易策略。"
                    "若不足，给出下一轮可执行搜索词。必须只输出 JSON。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"股票代码：{stock_code}\n"
                    "请返回 JSON："
                    '{"sufficient": true/false, "reason": "...", "followup_queries": ["..."]}。'
                    "若 sufficient=true，followup_queries 应为空数组。"
                    "followup_queries 最多 3 条，必须具体且可直接用于新闻检索。\n\n"
                    "当前信息：\n"
                    + "\n".join(context_lines)
                ),
            },
        ],
    }

    resp = request_with_retry(requests.post, url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    data = parse_json_object_from_text(content)
    sufficient = bool(data.get("sufficient", False))
    reason = str(data.get("reason", "")).strip()
    followup = data.get("followup_queries", [])
    if not isinstance(followup, list):
        followup = []
    clean_followup = list(dict.fromkeys(q.strip() for q in followup if isinstance(q, str) and q.strip()))[:3]
    return sufficient, clean_followup, reason


def refine_context_with_ai(config: Config, stock_code: str, contexts: list[dict], model_name: str) -> list[dict]:
    refined_contexts = list(contexts)
    for round_idx in range(1, config.search_reflection_max_rounds + 1):
        try:
            sufficient, followup_queries, reason = assess_information_sufficiency(
                config,
                stock_code,
                refined_contexts,
                model_name,
            )
        except Exception as exc:
            realtime_print(f"[进度] {stock_code}: 信息充分性评估失败，跳过追加检索: {exc}")
            break

        realtime_print(
            f"[进度] {stock_code}: 信息评估 第 {round_idx}/{config.search_reflection_max_rounds} 轮 -> "
            f"sufficient={sufficient} reason={reason or 'N/A'}"
        )
        if sufficient:
            break
        if not followup_queries:
            realtime_print(f"[进度] {stock_code}: 信息不足但未给出追加检索词，停止迭代")
            break

        realtime_print(f"[进度] {stock_code}: 触发追加检索，queries={followup_queries}")
        extra_hits = search_context_via_queries(
            stock_code,
            followup_queries,
            config.max_search_results,
            config.search_region,
        )
        if not extra_hits:
            realtime_print(f"[进度] {stock_code}: 追加检索未获得新结果，停止迭代")
            break

        before = len(refined_contexts)
        refined_contexts = merge_context_hits(refined_contexts, extra_hits)
        added = len(refined_contexts) - before
        realtime_print(f"[进度] {stock_code}: 追加检索新增 {added} 条上下文")
        if added <= 0:
            break

    return refined_contexts


def extract_followup_topics(results: list[dict], stock_code: str) -> list[str]:
    hot_words: list[str] = []
    for row in results[:20]:
        title = row.get("title", "")
        for token in re.findall(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}", title):
            token = token.strip()
            if token and token != stock_code and token not in hot_words:
                hot_words.append(token)
        if len(hot_words) >= 4:
            break
    return hot_words[:4]


def search_context_via_rss(stock_code: str, queries: list[str], max_results: int) -> list[dict]:
    import requests

    endpoints = [
        ("google-news-rss", "https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
        ("bing-news-rss", "https://www.bing.com/news/search?q={query}&format=rss&setlang=zh-cn"),
    ]
    collected: list[dict] = []
    with temporary_search_proxy_env(stock_code):
        for query in queries:
            for source_name, template in endpoints:
                url = template.format(query=quote_plus(query))
                try:
                    resp = request_with_retry(requests.get, url, timeout=20)
                    resp.raise_for_status()
                    items = parse_rss_items(resp.text)
                except Exception as exc:
                    realtime_print(f"[进度] {stock_code}: RSS 检索失败（{source_name}）: {exc}")
                    continue

                for item in items:
                    published = parse_datetime(item.get("published_at"))
                    if not published or not within_last_3_months(published):
                        continue
                    collected.append(
                        {
                            "query": query,
                            "region": source_name,
                            "title": item.get("title", ""),
                            "href": item.get("href", ""),
                            "body": item.get("body", ""),
                            "published_at": published.date().isoformat(),
                        }
                    )
                    if len(collected) >= max_results:
                        return collected
    return collected


def parse_bool_env(raw: str | None, default: bool = False) -> bool:
    value = (raw or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def temporary_search_proxy_env(stock_code: str):
    """已移除 VPN/Clash 代理逻辑，保留上下文管理器兼容旧调用。"""
    _ = stock_code
    return nullcontext()


def parse_rss_items(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    items: list[dict] = []
    for node in root.findall(".//item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        description = (node.findtext("description") or "").strip()
        pub_date = (node.findtext("pubDate") or "").strip()
        items.append({"title": title, "href": link, "body": description, "published_at": pub_date})
    return items


def build_user_prompt(stock_code: str, contexts: List[dict]) -> str:
    context_lines = []
    for i, item in enumerate(contexts, start=1):
        context_lines.append(
            f"[{i}] query={item['query']}\\ndate={item.get('published_at', 'unknown')}\\ntitle={item['title']}\\nurl={item['href']}\\nsummary={item['body']}"
        )

    context_blob = "\n\n".join(context_lines) if context_lines else "(无检索结果)"

    return textwrap.dedent(
        f"""
        你正在扮演“机构级股票研究员 + 短线操盘手 + 价值投资组合经理”，请基于以下关于股票 {stock_code} 的信息，输出严谨、可审计、可执行的投资研究结论。

        分析与合规要求：
        0) 只允许使用最近 3 个月内的新闻，且股价/技术指标必须按“最新可得数据”解读；若无法确认最新性，直接标注“数据不足”。
        1) 明确区分事实、推断、假设，不得把未经验证的信息当作事实。
        2) 必须分别给出“短线建议（1天~2周）”与“长线建议（3个月~3年）”。
        3) 每类建议都要包含：方向（买入/持有/减仓/观望）、建议仓位（百分比区间）、触发条件、失效条件、止损/风控、关键依据。
        4) 短线部分重点关注：趋势/量价/波动率结构/ATR/BOLL/ADX/MFI/OBV与事件催化；长线部分重点关注：基本面、行业景气度、估值、护城河与回撤风险。
        5) 必须输出“情景分析”：基准情景、乐观情景、悲观情景，并给出主观概率（合计 100%）。
        6) 必须输出“研究置信度（0-100）”与主要不确定性来源。
        7) 如果信息不足或冲突，明确写出不确定性与补充观察清单。
        8) 严禁保证收益，必须包含风险提示。
        9) 用简体中文，按下述模板结构化输出。

        输出模板：
        # {stock_code} 投研结论
        ## 一、核心结论（先给结论）
        ## 二、关键事实与数据新鲜度核验
        ## 三、短线策略（1天~2周）
        ## 四、长线策略（3个月~3年）
        ## 五、情景分析（基准/乐观/悲观 + 概率）
        ## 六、执行计划（入场、加减仓、止损、止盈、复盘观察点）
        ## 七、研究置信度与不确定性
        ## 八、风险提示（必须保留）

        检索信息：
        {context_blob}
        """
    ).strip()


def request_ai_advice(config: Config, stock_code: str, contexts: List[dict], model_name: str) -> str:
    url = f"{active_llm_base_url(config)}/chat/completions"
    headers = {
        "Authorization": f"Bearer {active_llm_api_key(config)}",
        "Content-Type": "application/json",
    }

    import requests

    payload = {
        "model": model_name,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是买方机构的首席策略分析师，兼具短线交易执行与价值投资研究能力。"
                    "你必须保持专业、中性、审慎：结论可执行、依据可追溯、风险可量化。"
                    "严禁承诺收益或使用煽动性表达。"
                "如果提供了结构化行情/指标快照，优先使用这些数据进行技术面分析。"
                "不要只停留在 MACD/KDJ，需结合 ATR、BOLL、ADX、MFI、OBV、年化波动率、回撤与支撑阻力给出可执行策略。"
                    "如证据不足，明确写出“数据不足”并降低置信度。"
                ),
            },
            {"role": "user", "content": build_user_prompt(stock_code, contexts)},
        ],
    }

    resp = request_with_retry(requests.post, url, headers=headers, json=payload, timeout=120)
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = json.dumps(resp.json(), ensure_ascii=False)
        except ValueError:
            detail = resp.text.strip()
        raise RuntimeError(
            f"{active_llm_label(config)} 请求失败（HTTP {resp.status_code}）。"
            f"请检查当前模型是否可用，当前值：{payload['model']}。"
            f"响应：{detail}"
        ) from exc
    data = resp.json()

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"{active_llm_label(config)} 返回格式异常: {json.dumps(data, ensure_ascii=False)}") from exc


def resolve_model(config: Config, requests_module) -> str:
    """优先使用用户配置模型，不可用时给出清晰报错与可选模型。"""
    url = f"{active_llm_base_url(config)}/models"
    headers = {"Authorization": f"Bearer {active_llm_api_key(config)}"}

    try:
        resp = request_with_retry(requests_module.get, url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        # /models 不可用时，不阻断主流程，回退到用户指定模型。
        return active_llm_model(config)

    model_ids = {
        item.get("id", "")
        for item in data.get("data", [])
        if isinstance(item, dict) and item.get("id")
    }

    configured_model = active_llm_model(config)
    if not model_ids or configured_model in model_ids:
        return configured_model

    preferred_candidates = [
        "gpt-4o",
        "gpt-4.1-mini",
        "deepseek-v3",
        "deepseek-r1",
        "claude-3-5-sonnet-latest",
    ]
    for candidate in preferred_candidates:
        if candidate in model_ids:
            return candidate

    first_available = sorted(model_ids)[0]
    return first_available


def run() -> None:
    parser = argparse.ArgumentParser(description="股票投资建议生成器（AIHUBMIX/NVIDIA NIM + DuckDuckGo）")
    parser.add_argument(
        "--pretty-json",
        action="store_true",
        help="额外输出结构化 JSON（包含检索信息与大模型回复）",
    )
    args = parser.parse_args()

    configure_realtime_stdout()
    config = load_config()
    import requests

    runtime_model_name = resolve_model(config, requests)
    realtime_print(f"[进度] 已加载配置，共 {len(config.stock_codes)} 只股票待分析")
    realtime_print(f"[进度] 当前 LLM: provider={active_llm_label(config)} model={runtime_model_name}")

    final_results: list[dict] = []
    research_cache: dict[str, StockResearchResult] = {}
    total = len(config.stock_codes)
    for index, code in enumerate(config.stock_codes, start=1):
        realtime_print(f"\n========== {code} ({index}/{total}) ==========")
        market_snapshot = fetch_market_snapshot(code, config)
        stock_name = detect_stock_name(code, market_snapshot)
        stock_label = format_stock_label(code, stock_name)
        if market_snapshot:
            realtime_print(f"[进度] {stock_label}: 行情源={market_snapshot.get('provider')} 时间={market_snapshot.get('timestamp')}")
        if config.chain_of_search_depth > 1:
            contexts = search_context_chain(code, config.max_search_results, config.search_region, config.chain_of_search_depth)
        else:
            contexts = search_context(code, config.max_search_results, config.search_region)
        if market_snapshot:
            contexts.insert(0, {
                "query": f"{code} 实时行情技术指标",
                "region": "direct-api",
                "title": f"{stock_label} 最新价格与技术指标（{market_snapshot.get('provider')}）",
                "href": market_snapshot.get("source_url", ""),
                "body": format_market_snapshot(market_snapshot),
                "published_at": market_snapshot.get("date", "unknown"),
            })
        contexts = refine_context_with_ai(config, code, contexts, runtime_model_name)
        if not stock_name:
            stock_name = detect_stock_name_from_contexts(code, contexts)
            stock_label = format_stock_label(code, stock_name)
        realtime_print(f"[进度] {stock_label}: 开始请求 AI 生成建议")
        advice = request_ai_advice(config, code, contexts, runtime_model_name)
        realtime_print(f"[进度] {stock_label}: AI 建议生成完成")
        realtime_print(advice)
        brief_summary = build_brief_summary_with_ai(config, code, advice, runtime_model_name)
        research_cache[code] = StockResearchResult(
            stock_code=code,
            stock_name=stock_name,
            contexts=contexts,
            advice=advice,
            brief_summary=brief_summary,
        )
        final_results.append(build_result_dict(research_cache[code]))

    if config.email_stock_router:
        send_group_emails(config, research_cache)

    if args.pretty_json:
        realtime_print("\n========== JSON ==========")
        realtime_print(json.dumps(final_results, ensure_ascii=False, indent=2))


def parse_published_at(hit: dict) -> datetime | None:
    """尽力解析检索结果时间，无法解析则返回 None。"""
    candidates = [
        hit.get("date"),
        hit.get("published"),
        hit.get("published_at"),
        hit.get("datetime"),
        hit.get("body"),
    ]
    for candidate in candidates:
        dt = parse_datetime(candidate)
        if dt:
            return dt
    return None


def parse_datetime(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    text = str(raw_value).strip()
    if not text:
        return None

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    patterns = [
        r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        year, month, day = map(int, match.groups())
        try:
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None

    return None


def within_last_3_months(dt: datetime) -> bool:
    now = now_shanghai()
    cutoff = now - timedelta(days=92)
    return dt >= cutoff


def send_group_emails(config: Config, research_by_stock: dict[str, StockResearchResult]) -> None:
    protocol = config.email_delivery_protocol
    if protocol == "exchange":
        send_group_emails_via_exchange(config, research_by_stock)
        return

    # smtp / pop3 / imap / carddav 场景统一走 SMTP 投递，兼容常见邮箱服务商。
    send_group_emails_via_smtp(config, research_by_stock)


def send_group_emails_via_smtp(config: Config, research_by_stock: dict[str, StockResearchResult]) -> None:
    if not config.sender_email or not config.sender_auth_code:
        raise ValueError("已配置 EMAIL_STOCK_ROUTER，但缺少 SENDER_EMAIL 或 SENDER_AUTH_CODE")
    if not config.smtp_host:
        raise ValueError("无法确定 SMTP_HOST，请设置环境变量 SMTP_HOST")

    if config.smtp_security == "ssl":
        smtp = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30)
    else:
        smtp = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)

    with smtp:
        if config.smtp_security == "starttls":
            smtp.starttls()
        smtp.login(config.sender_email, config.sender_auth_code)
        for receiver, stocks in config.email_stock_router.items():
            message = build_email_message(config, receiver, stocks, research_by_stock)
            if message is None:
                continue
            smtp.sendmail(config.sender_email, [receiver], message.as_string())
            realtime_print(f"[进度] 邮件发送完成 -> {receiver} ({len(stocks)} 只股票)")


def send_group_emails_via_exchange(config: Config, research_by_stock: dict[str, StockResearchResult]) -> None:
    import requests

    required = [
        config.exchange_tenant_id,
        config.exchange_client_id,
        config.exchange_client_secret,
        config.exchange_sender_upn,
    ]
    if not all(required):
        raise ValueError("EMAIL_DELIVERY_PROTOCOL=exchange 时，需配置 EXCHANGE_TENANT_ID/EXCHANGE_CLIENT_ID/EXCHANGE_CLIENT_SECRET/EXCHANGE_SENDER_UPN")

    token_url = f"https://login.microsoftonline.com/{config.exchange_tenant_id}/oauth2/v2.0/token"
    token_resp = request_with_retry(
        requests.post,
        token_url,
        data={
            "client_id": config.exchange_client_id,
            "client_secret": config.exchange_client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    token_resp.raise_for_status()
    access_token = token_resp.json().get("access_token")
    if not access_token:
        raise RuntimeError("Exchange 鉴权失败：未获取 access_token")

    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    for receiver, stocks in config.email_stock_router.items():
        message = build_email_message(config, receiver, stocks, research_by_stock)
        if message is None:
            continue
        graph_url = f"https://graph.microsoft.com/v1.0/users/{config.exchange_sender_upn}/sendMail"
        payload = {
            "message": {
                "subject": message["Subject"],
                "body": {"contentType": "HTML", "content": extract_html_body(message)},
                "toRecipients": [{"emailAddress": {"address": receiver}}],
            },
            "saveToSentItems": True,
        }
        resp = request_with_retry(requests.post, graph_url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        realtime_print(f"[进度] Exchange 邮件发送完成 -> {receiver} ({len(stocks)} 只股票)")


def build_email_message(
    config: Config,
    receiver: str,
    stocks: list[str],
    research_by_stock: dict[str, StockResearchResult],
) -> MIMEMultipart | None:
    html_sections: list[str] = []
    quick_summary_items: list[str] = []
    plain_lines = ["以下为今日股票分析：", ""]
    for code in stocks:
        research = research_by_stock.get(code)
        if not research:
            continue
        advice = research.advice
        safe_advice = markdown_to_html(advice)
        summary = research.brief_summary
        stock_label = format_stock_label(code, research.stock_name)
        quick_summary_items.append(
            "<li style='margin:8px 0;line-height:1.6;'>"
            f"<strong style='color:#0f172a;'>{html.escape(stock_label)}</strong>"
            f"<div style='margin-top:4px;color:#334155;'>{html.escape(summary)}</div>"
            "</li>"
        )
        html_sections.append(
            "<section style='margin:16px 0;padding:14px;border:1px solid #e2e8f0;border-radius:12px;background:#ffffff;'>"
            f"<h3 style='margin:0 0 10px 0;color:#0f172a;font-size:16px;'>{html.escape(stock_label)}</h3>"
            f"<div style='line-height:1.7;color:#1f2937;font-size:14px;'>{safe_advice}</div>"
            "</section>"
        )
        plain_lines.extend([f"- {stock_label}：{summary}", ""])

    for code in stocks:
        research = research_by_stock.get(code)
        if not research:
            continue
        plain_lines.extend([f"## {format_stock_label(code, research.stock_name)}", research.advice, ""])

    if not html_sections:
        return None

    summary_rows = "".join(
        "<tr>"
        f"<td style='padding:10px 12px;border-bottom:1px solid #eef2f7;'>{html.escape(format_stock_label(code, research_by_stock[code].stock_name))}</td>"
        f"<td style='padding:10px 12px;border-bottom:1px solid #eef2f7;'>{len(research_by_stock.get(code, StockResearchResult(code, None, [], '', '信息不足')).contexts)}</td>"
        "<td style='padding:10px 12px;border-bottom:1px solid #eef2f7;'>✅ 已完成</td>"
        "</tr>"
        for code in stocks
        if code in research_by_stock
    )

    html_body = (
        "<html><body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f1f5f9;padding:18px;'>"
        "<div style='max-width:900px;margin:0 auto;background:#ffffff;border:1px solid #dbe2ea;border-radius:14px;overflow:hidden;'>"
        "<div style='padding:18px 20px;background:linear-gradient(120deg,#0f172a,#1d4ed8);color:#ffffff;'>"
        "<h2 style='margin:0 0 6px 0;font-size:22px;'>📈 股票分析日报</h2>"
        f"<p style='margin:0;font-size:13px;opacity:0.9;'>⏰ {now_shanghai().strftime('%Y-%m-%d %H:%M:%S')} ｜ 📬 {html.escape(receiver)}</p>"
        "</div>"
        "<div style='padding:16px 18px 6px 18px;'>"
        "<table style='width:100%;border-collapse:collapse;margin:0 0 14px 0;font-size:13px;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;'>"
        "<thead><tr style='background:#f8fafc;color:#334155;'>"
        "<th style='text-align:left;padding:10px 12px;'>股票</th>"
        "<th style='text-align:left;padding:10px 12px;'>样本条数</th>"
        "<th style='text-align:left;padding:10px 12px;'>状态</th>"
        "</tr></thead>"
        f"<tbody>{summary_rows}</tbody></table>"
        "<div style='margin:0 0 14px 0;padding:12px 14px;background:#f8fafc;border:1px solid #dbeafe;border-radius:10px;'>"
        "<p style='margin:0 0 8px 0;font-weight:700;color:#0f172a;'>🧾 AI 快速摘要</p>"
        "<ul style='margin:0;padding-left:20px;color:#334155;font-size:14px;'>"
        + "".join(quick_summary_items)
        + "</ul></div>"
        "<p style='margin:0 0 8px 0;color:#475569;font-size:13px;'>📌 阅读顺序：核心结论 → 执行计划 → 风险提示。</p>"
        + "".join(html_sections)
        + "<p style='margin:14px 0 18px 0;color:#64748b;font-size:12px;'>"
        "⚠️ 风险提示：以上内容仅供参考，不构成任何投资建议，请严格做好仓位与止损管理。"
        "</p></div></div></body></html>"
    )

    message = MIMEMultipart("alternative")
    message.attach(MIMEText("\n".join(plain_lines), "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))
    message["Subject"] = "股票分析日报"
    message["From"] = formataddr(("Stock Adviser", config.sender_email or config.exchange_sender_upn or ""))
    message["To"] = receiver
    return message


def extract_html_body(message: MIMEMultipart) -> str:
    for part in message.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""

def build_result_dict(research: StockResearchResult) -> dict:
    normalized_stock_name = (research.stock_name or "").strip() or research.stock_code
    return {
        "stock_code": research.stock_code,
        "stock_name": normalized_stock_name,
        "search_context_count": len(research.contexts),
        "brief_summary": research.brief_summary,
        "advice": research.advice,
    }


def format_stock_label(stock_code: str, stock_name: str | None) -> str:
    name = (stock_name or "").strip()
    if name:
        return f"{stock_code}（{name}）"
    return stock_code


def detect_stock_name(stock_code: str, market_snapshot: dict | None) -> str | None:
    if market_snapshot:
        for key in ("stock_name", "name", "short_name"):
            value = market_snapshot.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    detected = fetch_cn_stock_name(stock_code)
    if detected:
        return detected
    return fetch_yahoo_stock_name(stock_code)


def detect_stock_name_from_contexts(stock_code: str, contexts: list[dict]) -> str | None:
    aliases = {alias.upper() for alias in stock_code_aliases(stock_code)}
    yahoo_symbol = to_yahoo_symbol(stock_code).upper()
    if yahoo_symbol:
        aliases.add(yahoo_symbol)

    for context in contexts:
        for field in ("title", "body"):
            raw_text = context.get(field, "")
            if not isinstance(raw_text, str) or not raw_text.strip():
                continue
            extracted = extract_stock_name_from_text(raw_text, aliases)
            if extracted:
                return extracted
    return None


def extract_stock_name_from_text(text: str, aliases: set[str]) -> str | None:
    if not aliases:
        return None

    for alias in aliases:
        patterns = [
            rf"([A-Za-z一-鿿][A-Za-z0-9一-鿿&.\-\s]{{1,40}}?)\s*[（(\[]\s*{re.escape(alias)}\s*[)）\]]",
            rf"{re.escape(alias)}\s*[（(\[]\s*([A-Za-z一-鿿][A-Za-z0-9一-鿿&.\-\s]{{1,40}}?)\s*[)）\]]",
            rf"{re.escape(alias)}\s*[-—:：]\s*([A-Za-z一-鿿][A-Za-z0-9一-鿿&.\-\s]{{1,40}})",
            rf"([A-Za-z一-鿿][A-Za-z0-9一-鿿&.\-\s]{{1,40}})\s*[-—:：]\s*{re.escape(alias)}",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = clean_extracted_stock_name(match.group(1))
            if candidate:
                return candidate
    return None


def clean_extracted_stock_name(candidate: str) -> str | None:
    name = re.sub(r"\s+", " ", candidate).strip(" -—:：,，。;；()（）[]【】")
    if not name:
        return None
    lowered = name.lower()
    blocked = {"stock", "shares", "quote", "news", "analysis", "finance", "股票", "行情", "分析"}
    if lowered in blocked:
        return None
    if len(name) < 2:
        return None
    return name


def fetch_cn_stock_name(stock_code: str) -> str | None:
    if not stock_code.isdigit() or len(stock_code) != 6:
        return None
    try:
        import requests

        secid, symbol = to_eastmoney_secid(stock_code)
        exchange = "sh" if secid.startswith("1.") else "sz"
        url = f"https://hq.sinajs.cn/list={exchange}{symbol}"
        response = request_with_retry(requests.get, url, timeout=10)
        response.raise_for_status()
        parts = response.text.split('"')
        if len(parts) < 2:
            return None
        values = parts[1].split(",")
        if not values:
            return None
        name = values[0].strip()
        return name or None
    except Exception:
        return None


def fetch_yahoo_stock_name(stock_code: str) -> str | None:
    symbol = to_yahoo_symbol(stock_code)
    if not symbol:
        return None
    try:
        import requests

        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={quote_plus(symbol)}"
        response = request_with_retry(requests.get, url, timeout=10)
        response.raise_for_status()
        data = response.json()
        result = (data.get("quoteResponse") or {}).get("result") or []
        if not result:
            return None
        quote = result[0] if isinstance(result[0], dict) else {}
        name = quote.get("shortName") or quote.get("longName")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None
    except Exception:
        return None



def build_brief_summary_with_ai(config: Config, stock_code: str, advice: str, model_name: str) -> str:
    ai_summary = request_ai_brief_summary(config, stock_code, advice, model_name)
    if ai_summary:
        return ai_summary
    return build_brief_summary(stock_code, advice)


def request_ai_brief_summary(config: Config, stock_code: str, advice: str, model_name: str) -> str | None:
    url = f"{active_llm_base_url(config)}/chat/completions"
    headers = {
        "Authorization": f"Bearer {active_llm_api_key(config)}",
        "Content-Type": "application/json",
    }

    import requests

    payload = {
        "model": model_name,
        "temperature": 0.1,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是专业投研摘要助手。"
                    "请将长文压缩为高可信、可执行的极简摘要，禁止夸张表达。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"请将 {stock_code} 的投研结论浓缩成 1 句中文摘要（25~60字）。"
                    "要求包含：方向、仓位/动作、最大风险点。"
                    "仅输出摘要本身，不要编号和前后缀。\n\n"
                    f"原文：\n{advice}"
                ),
            },
        ],
    }

    try:
        resp = request_with_retry(requests.post, url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        content = re.sub(r"\s+", " ", content)
        return content[:120]
    except Exception as exc:
        realtime_print(f"[进度] {stock_code}: AI 摘要生成失败，回退规则摘要: {exc}")
        return None
def build_brief_summary(stock_code: str, advice: str) -> str:
    direction = extract_with_patterns(
        advice,
        [r"看多", r"偏多", r"买入", r"增持", r"看空", r"偏空", r"减仓", r"中性", r"观望", r"持有"],
    )
    confidence = extract_with_regex(advice, r"(?:研究)?置信度[^0-9]*(\d{1,3})")
    trend_strength = extract_with_regex(advice, r"趋势强度[^0-9]*(\d{1,3})")
    position_action = extract_with_patterns(advice, [r"加仓", r"减仓", r"持有", r"观望", r"买入", r"止盈", r"止损"])

    parts = []
    if direction:
        parts.append(f"观点={direction}")
    if confidence:
        parts.append(f"置信度={confidence}/100")
    if trend_strength:
        parts.append(f"趋势强度={trend_strength}/100")
    if position_action:
        parts.append(f"仓位动作={position_action}")

    if not parts:
        first_line = next((line.strip("-• ") for line in advice.splitlines() if line.strip()), "信息不足")
        first_line = re.sub(r"\s+", " ", first_line)
        return f"{first_line[:70]}{'…' if len(first_line) > 70 else ''}"

    return "；".join(parts)


def extract_with_patterns(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return pattern
    return None


def extract_with_regex(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1)


def markdown_to_html(markdown_text: str) -> str:
    """轻量 Markdown 转 HTML，避免邮件客户端把 Markdown 当纯文本显示。"""
    if not markdown_text.strip():
        return "<p>（无内容）</p>"

    lines = markdown_text.splitlines()
    blocks: list[str] = []
    in_list = False
    in_table = False
    table_headers: list[str] = []

    def close_table() -> None:
        nonlocal in_table, table_headers
        if in_table:
            blocks.append("</tbody></table>")
            in_table = False
            table_headers = []

    for raw in lines:
        line = raw.strip()
        if not line:
            if in_list:
                blocks.append("</ul>")
                in_list = False
            close_table()
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            if in_list:
                blocks.append("</ul>")
                in_list = False
            close_table()
            level = min(4, len(heading_match.group(1)) + 1)
            content = apply_inline_markdown(heading_match.group(2))
            blocks.append(f"<h{level} style='margin:10px 0 6px 0;'>{content}</h{level}>")
            continue

        if line.startswith("|") and line.endswith("|"):
            cols = [c.strip() for c in line.strip("|").split("|")]
            is_delimiter = all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in cols)
            if is_delimiter:
                continue
            if in_list:
                blocks.append("</ul>")
                in_list = False
            if not in_table:
                table_headers = cols
                blocks.append("<table style='width:100%;border-collapse:collapse;margin:8px 0;font-size:13px;'>")
                blocks.append("<thead><tr style='background:#f3f4f6;'>")
                for col in table_headers:
                    blocks.append(f"<th style='text-align:left;padding:6px;border:1px solid #e5e7eb;'>{apply_inline_markdown(col)}</th>")
                blocks.append("</tr></thead><tbody>")
                in_table = True
            else:
                blocks.append("<tr>")
                for col in cols:
                    blocks.append(f"<td style='padding:6px;border:1px solid #e5e7eb;'>{apply_inline_markdown(col)}</td>")
                blocks.append("</tr>")
            continue

        list_match = re.match(r"^[-*]\s+(.+)$", line)
        if list_match:
            close_table()
            if not in_list:
                blocks.append("<ul style='margin:6px 0 8px 20px;padding:0;'>")
                in_list = True
            blocks.append(f"<li style='margin:2px 0;'>{apply_inline_markdown(list_match.group(1))}</li>")
            continue

        if in_list:
            blocks.append("</ul>")
            in_list = False
        blocks.append(f"<p style='margin:6px 0;'>{apply_inline_markdown(line)}</p>")

    if in_list:
        blocks.append("</ul>")
    close_table()

    return "".join(blocks)


def apply_inline_markdown(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code style='background:#f3f4f6;padding:0 4px;border-radius:4px;'>\1</code>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<a href='\2'>\1</a>", escaped)
    return escaped


def fetch_market_snapshot(stock_code: str, config: Config) -> dict | None:
    providers = [config.market_data_provider]
    if config.market_data_provider == "auto":
        providers = ["akshare", "eastmoney", "tencent", "sina", "yahoo", "stooq"] if stock_code.isdigit() else ["yahoo", "stooq", "akshare", "eastmoney", "tencent", "sina"]

    for provider in providers:
        try:
            if provider == "yahoo":
                snapshot = fetch_market_snapshot_from_yahoo(stock_code)
            elif provider == "akshare":
                snapshot = fetch_market_snapshot_from_akshare(stock_code)
            elif provider == "sina":
                snapshot = fetch_market_snapshot_from_sina(stock_code)
            elif provider == "tencent":
                snapshot = fetch_market_snapshot_from_tencent(stock_code)
            elif provider == "stooq":
                snapshot = fetch_market_snapshot_from_stooq(stock_code)
            else:
                snapshot = fetch_market_snapshot_from_eastmoney(stock_code)
        except Exception as exc:
            realtime_print(f"[进度] {stock_code}: {provider} 行情抓取失败: {exc}")
            snapshot = None

        if snapshot:
            return snapshot
    return None


def fetch_market_snapshot_from_yahoo(stock_code: str) -> dict | None:
    import requests

    symbol = to_yahoo_symbol(stock_code)
    target_trade_date = nearest_open_trade_date()
    quote_url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    quote_resp = request_with_retry(requests.get, quote_url, headers=headers, timeout=20)
    quote_resp.raise_for_status()
    quote_results = quote_resp.json().get("quoteResponse", {}).get("result", [])
    quote = quote_results[0] if quote_results else {}

    chart_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=6mo&interval=1d"
    chart_resp = request_with_retry(requests.get, chart_url, headers=headers, timeout=20)
    chart_resp.raise_for_status()
    chart_result = chart_resp.json().get("chart", {}).get("result", [])
    if not chart_result:
        return None

    chart = chart_result[0]
    timestamps = chart.get("timestamp", [])
    quote_data = chart.get("indicators", {}).get("quote", [{}])[0]
    raw_closes = quote_data.get("close", [])
    raw_highs = quote_data.get("high", [])
    raw_lows = quote_data.get("low", [])
    raw_volumes = quote_data.get("volume", [])
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    volumes: list[float] = []
    selected_price = None
    selected_timestamp = None
    for idx, (ts, close) in enumerate(zip(timestamps, raw_closes)):
        if not isinstance(close, (int, float)):
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        if dt.date() <= target_trade_date:
            closes.append(float(close))
            if idx < len(raw_highs) and isinstance(raw_highs[idx], (int, float)):
                highs.append(float(raw_highs[idx]))
            if idx < len(raw_lows) and isinstance(raw_lows[idx], (int, float)):
                lows.append(float(raw_lows[idx]))
            if idx < len(raw_volumes) and isinstance(raw_volumes[idx], (int, float)):
                volumes.append(float(raw_volumes[idx]))
            selected_price = float(close)
            selected_timestamp = dt.isoformat()

    if not closes:
        closes = [v for v in raw_closes if isinstance(v, (int, float))]
    indicators = calculate_indicators(closes, highs=highs if len(highs)==len(closes) else None, lows=lows if len(lows)==len(closes) else None, volumes=volumes if len(volumes)==len(closes) else None)

    return {
        "provider": "yahoo",
        "symbol": symbol,
        "stock_name": quote.get("shortName") or quote.get("longName"),
        "price": selected_price or quote.get("regularMarketPrice"),
        "change_percent": quote.get("regularMarketChangePercent"),
        "timestamp": selected_timestamp or to_iso_timestamp(quote.get("regularMarketTime")),
        "date": target_trade_date.isoformat(),
        "source_url": f"https://finance.yahoo.com/quote/{symbol}",
        "trade_date": target_trade_date.isoformat(),
        **indicators,
    }


def fetch_market_snapshot_from_akshare(stock_code: str) -> dict | None:
    import akshare as ak

    if not stock_code.isdigit() or len(stock_code) != 6:
        return None

    target_trade_date = nearest_open_trade_date()
    end_date = target_trade_date.strftime("%Y%m%d")
    start_date = (target_trade_date - timedelta(days=240)).strftime("%Y%m%d")

    df = None
    last_exception = None
    for adjust in ["qfq", ""]:
        for attempt in range(1, 4):
            try:
                df = ak.stock_zh_a_hist(
                    symbol=stock_code,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust=adjust,
                )
                if df is not None and not df.empty:
                    break
            except Exception as exc:
                last_exception = exc
                time.sleep(0.8 * attempt)
        if df is not None and not df.empty:
            break

    if (df is None or df.empty) and last_exception is not None:
        realtime_print(f"[进度] {stock_code}: akshare 历史行情重试后仍失败: {last_exception}")
    if df is None or df.empty:
        return None

    if "日期" not in df.columns or "收盘" not in df.columns:
        return None

    df = df.copy()
    df["日期"] = df["日期"].astype(str)
    target_str = target_trade_date.strftime("%Y-%m-%d")
    df = df[df["日期"] <= target_str]
    if df.empty:
        return None

    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    volumes: list[float] = []
    for row in df.to_dict("records"):
        value = row.get("收盘")
        parsed = safe_float(value)
        if parsed is not None:
            closes.append(parsed)
            high = safe_float(row.get("最高"))
            low = safe_float(row.get("最低"))
            vol = safe_float(row.get("成交量"))
            highs.append(high if high is not None else parsed)
            lows.append(low if low is not None else parsed)
            volumes.append(vol if vol is not None else 0.0)
    if not closes:
        return None

    last_row = df.iloc[-1]
    indicators = calculate_indicators(closes, highs=highs, lows=lows, volumes=volumes)
    return {
        "provider": "akshare",
        "symbol": stock_code,
        "price": float(last_row["收盘"]),
        "change_percent": safe_float(last_row.get("涨跌幅")),
        "timestamp": f"{last_row['日期']}T15:00:00+08:00",
        "date": last_row["日期"],
        "trade_date": target_trade_date.isoformat(),
        "source_url": f"https://quote.eastmoney.com/{stock_code}.html",
        **indicators,
    }


def fetch_market_snapshot_from_sina(stock_code: str) -> dict | None:
    import requests

    secid, symbol = to_eastmoney_secid(stock_code)
    exchange = "sh" if secid.startswith("1.") else "sz"
    url = f"https://hq.sinajs.cn/list={exchange}{symbol}"
    resp = request_with_retry(requests.get, url, timeout=20)
    resp.raise_for_status()
    text = resp.text
    parts = text.split("\"")
    if len(parts) < 2:
        return None
    values = parts[1].split(",")
    if len(values) < 4:
        return None
    price = safe_float(values[3])
    prev_close = safe_float(values[2])
    change_percent = None
    if price is not None and prev_close and not math.isclose(prev_close, 0.0):
        change_percent = (price - prev_close) / prev_close * 100

    bars = fetch_recent_bars_from_eastmoney(stock_code)
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b["volume"] for b in bars]
    indicators = calculate_indicators(closes, highs=highs, lows=lows, volumes=volumes)
    return {
        "provider": "sina",
        "symbol": symbol,
        "stock_name": values[0].strip() if values else None,
        "price": price,
        "change_percent": change_percent,
        "timestamp": now_shanghai().isoformat(),
        "date": now_shanghai().date().isoformat(),
        "source_url": f"https://finance.sina.com.cn/realstock/company/{exchange}{symbol}/nc.shtml",
        **indicators,
    }


def fetch_market_snapshot_from_tencent(stock_code: str) -> dict | None:
    import requests

    secid, symbol = to_eastmoney_secid(stock_code)
    exchange = "sh" if secid.startswith("1.") else "sz"
    url = f"https://qt.gtimg.cn/q={exchange}{symbol}"
    resp = request_with_retry(requests.get, url, timeout=20)
    resp.raise_for_status()
    parts = resp.text.split("~")
    if len(parts) < 40:
        return None
    price = safe_float(parts[3])
    change_percent = safe_float(parts[32])
    bars = fetch_recent_bars_from_eastmoney(stock_code)
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b["volume"] for b in bars]
    indicators = calculate_indicators(closes, highs=highs, lows=lows, volumes=volumes)
    return {
        "provider": "tencent",
        "symbol": symbol,
        "stock_name": parts[1].strip() if len(parts) > 1 else None,
        "price": price,
        "change_percent": change_percent,
        "timestamp": now_shanghai().isoformat(),
        "date": now_shanghai().date().isoformat(),
        "source_url": f"https://gu.qq.com/{exchange}{symbol}",
        **indicators,
    }


def fetch_market_snapshot_from_stooq(stock_code: str) -> dict | None:
    import requests

    symbol = to_yahoo_symbol(stock_code).replace(".SS", ".CN").replace(".SZ", ".CN")
    url = f"https://stooq.com/q/d/l/?s={symbol.lower()}&i=d"
    resp = request_with_retry(requests.get, url, timeout=20)
    resp.raise_for_status()
    lines = [line.strip() for line in resp.text.splitlines() if line.strip()]
    if len(lines) < 3:
        return None
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    volumes: list[float] = []
    last_close = None
    last_date = None
    for row in lines[1:]:
        cols = row.split(",")
        if len(cols) < 5:
            continue
        high = safe_float(cols[2])
        low = safe_float(cols[3])
        close = safe_float(cols[4])
        vol = safe_float(cols[5]) if len(cols) > 5 else None
        if close is None:
            continue
        closes.append(close)
        highs.append(high if high is not None else close)
        lows.append(low if low is not None else close)
        volumes.append(vol if vol is not None else 0.0)
        last_close = close
        last_date = cols[0]
    if not closes:
        return None
    indicators = calculate_indicators(closes, highs=highs, lows=lows, volumes=volumes)
    return {
        "provider": "stooq",
        "symbol": symbol,
        "price": last_close,
        "change_percent": None,
        "timestamp": now_shanghai().isoformat(),
        "date": last_date or now_shanghai().date().isoformat(),
        "source_url": f"https://stooq.com/q/?s={symbol.lower()}",
        **indicators,
    }


def fetch_recent_bars_from_eastmoney(stock_code: str) -> list[dict]:
    import requests

    secid, _ = to_eastmoney_secid(stock_code)
    kline_url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
        "&klt=101&fqt=1&lmt=180"
    )
    kline_resp = request_with_retry(requests.get, kline_url, timeout=20)
    kline_resp.raise_for_status()
    klines = kline_resp.json().get("data", {}).get("klines", [])

    bars: list[dict] = []
    for row in klines:
        parts = row.split(",")
        if len(parts) < 6:
            continue
        close = safe_float(parts[2])
        high = safe_float(parts[3])
        low = safe_float(parts[4])
        volume = safe_float(parts[5])
        if close is not None:
            bars.append({"close": close, "high": high if high is not None else close, "low": low if low is not None else close, "volume": volume if volume is not None else 0.0})
    return bars


def fetch_market_snapshot_from_eastmoney(stock_code: str) -> dict | None:
    import requests

    secid, symbol = to_eastmoney_secid(stock_code)
    quote_url = (
        "https://push2.eastmoney.com/api/qt/stock/get?"
        f"secid={secid}&fields=f43,f44,f45,f46,f47,f48,f49,f57,f58,f60,f169,f170"
    )
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    quote_resp = request_with_retry(requests.get, quote_url, headers=headers, timeout=20)
    quote_resp.raise_for_status()
    data = quote_resp.json().get("data")
    if not data:
        return None

    kline_url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
        "&klt=101&fqt=1&lmt=180"
    )
    kline_resp = request_with_retry(requests.get, kline_url, timeout=20)
    kline_resp.raise_for_status()
    klines = kline_resp.json().get("data", {}).get("klines", [])

    bars = fetch_recent_bars_from_eastmoney(stock_code)
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b["volume"] for b in bars]
    indicators = calculate_indicators(closes, highs=highs, lows=lows, volumes=volumes)
    price = safe_divide(data.get("f43"), 100)
    change_percent = safe_divide(data.get("f170"), 100)

    return {
        "provider": "eastmoney",
        "symbol": symbol,
        "stock_name": data.get("f58"),
        "price": price,
        "change_percent": change_percent,
        "timestamp": now_shanghai().isoformat(),
        "date": now_shanghai().date().isoformat(),
        "source_url": f"https://quote.eastmoney.com/{symbol}.html",
        **indicators,
    }



def normalize_stock_code(stock_code: str) -> str:
    code = stock_code.strip().upper()
    if not code:
        return code

    for prefix in ("US.", "NASDAQ.", "NYSE."):
        if code.startswith(prefix) and len(code) > len(prefix):
            return code[len(prefix):]

    us_suffix = re.fullmatch(r"([A-Z][A-Z0-9\-]{0,9})\.US", code)
    if us_suffix:
        return us_suffix.group(1)

    a_share_prefix = re.fullmatch(r"(SH|SZ)\.?([0-9]{6})", code)
    if a_share_prefix:
        return a_share_prefix.group(2)

    a_share_suffix = re.fullmatch(r"([0-9]{6})\.(SH|SZ)", code)
    if a_share_suffix:
        return a_share_suffix.group(1)

    return code



def to_iso_timestamp(value: object) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def to_yahoo_symbol(stock_code: str) -> str:
    code = normalize_stock_code(stock_code)
    if "." in code:
        return code
    if code.isdigit() and len(code) == 6:
        suffix = "SS" if code.startswith("6") else "SZ"
        return f"{code}.{suffix}"
    if code.isdigit() and len(code) in {4, 5}:
        return f"{code.zfill(4)}.HK"
    return code


def to_eastmoney_secid(stock_code: str) -> tuple[str, str]:
    code = normalize_stock_code(stock_code)
    if code.isdigit() and len(code) == 6:
        market = "1" if code.startswith("6") else "0"
        return f"{market}.{code}", code
    yahoo = to_yahoo_symbol(code)
    if yahoo.endswith(".SS"):
        raw = yahoo.replace(".SS", "")
        return f"1.{raw}", raw
    if yahoo.endswith(".SZ"):
        raw = yahoo.replace(".SZ", "")
        return f"0.{raw}", raw
    raise ValueError("东方财富接口暂仅支持 A 股 6 位代码")


def calculate_indicators(
    closes: list[float],
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[float] | None = None,
) -> dict:
    if len(closes) < 35:
        return {
            "rsi14": None,
            "macd": None,
            "macd_signal": None,
            "macd_hist": None,
            "kdj_k": None,
            "kdj_d": None,
            "kdj_j": None,
            "sma20": None,
            "sma60": None,
            "ema20": None,
            "boll_upper": None,
            "boll_mid": None,
            "boll_lower": None,
            "volatility20": None,
            "momentum20": None,
            "max_drawdown120": None,
            "support20": None,
            "resistance20": None,
            "trend_strength": None,
            "atr14": None,
            "obv": None,
        }

    rsi = calculate_rsi(closes, period=14)
    macd, signal, hist = calculate_macd(closes)
    k, d, j = calculate_kdj(closes, period=9)
    sma20 = simple_moving_average(closes, 20)
    sma60 = simple_moving_average(closes, 60)
    ema20 = ema_series(closes, 20)[-1] if len(closes) >= 20 else None
    boll_upper, boll_mid, boll_lower = calculate_bollinger(closes, period=20)
    volatility20 = calculate_volatility(closes, period=20)
    momentum20 = calculate_momentum(closes, period=20)
    max_drawdown120 = calculate_max_drawdown(closes[-120:])
    support20 = min(closes[-20:]) if len(closes) >= 20 else None
    resistance20 = max(closes[-20:]) if len(closes) >= 20 else None
    trend_strength = None
    atr14 = calculate_atr(highs or closes, lows or closes, closes, period=14)
    obv = calculate_obv(closes, volumes) if volumes else None
    ema60 = ema_series(closes, 60)[-1] if len(closes) >= 60 else None
    if ema20 and ema60 and not math.isclose(ema60, 0.0):
        trend_strength = (ema20 - ema60) / ema60

    return {
        "rsi14": round(rsi, 2) if rsi is not None else None,
        "macd": round(macd, 4) if macd is not None else None,
        "macd_signal": round(signal, 4) if signal is not None else None,
        "macd_hist": round(hist, 4) if hist is not None else None,
        "kdj_k": round(k, 2) if k is not None else None,
        "kdj_d": round(d, 2) if d is not None else None,
        "kdj_j": round(j, 2) if j is not None else None,
        "sma20": round(sma20, 4) if sma20 is not None else None,
        "sma60": round(sma60, 4) if sma60 is not None else None,
        "ema20": round(ema20, 4) if ema20 is not None else None,
        "boll_upper": round(boll_upper, 4) if boll_upper is not None else None,
        "boll_mid": round(boll_mid, 4) if boll_mid is not None else None,
        "boll_lower": round(boll_lower, 4) if boll_lower is not None else None,
        "volatility20": round(volatility20, 4) if volatility20 is not None else None,
        "momentum20": round(momentum20, 4) if momentum20 is not None else None,
        "max_drawdown120": round(max_drawdown120, 4) if max_drawdown120 is not None else None,
        "support20": round(support20, 4) if support20 is not None else None,
        "resistance20": round(resistance20, 4) if resistance20 is not None else None,
        "trend_strength": round(trend_strength, 4) if trend_strength is not None else None,
        "atr14": round(atr14, 4) if atr14 is not None else None,
        "obv": round(obv, 4) if obv is not None else None,
    }


def calculate_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1 or len(highs) != len(closes) or len(lows) != len(closes):
        return None
    true_ranges: list[float] = []
    for i in range(1, len(closes)):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    if len(true_ranges) < period:
        return None
    return sum(true_ranges[-period:]) / period


def calculate_obv(closes: list[float], volumes: list[float]) -> float | None:
    if len(closes) < 2 or len(volumes) != len(closes):
        return None
    obv = 0.0
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv += volumes[i]
        elif closes[i] < closes[i - 1]:
            obv -= volumes[i]
    return obv


def simple_moving_average(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def calculate_bollinger(values: list[float], period: int = 20, std_multiplier: float = 2.0) -> tuple[float | None, float | None, float | None]:
    if len(values) < period:
        return None, None, None
    window = values[-period:]
    mid = sum(window) / period
    variance = sum((v - mid) ** 2 for v in window) / period
    std = math.sqrt(variance)
    return mid + std_multiplier * std, mid, mid - std_multiplier * std


def calculate_volatility(values: list[float], period: int = 20) -> float | None:
    if len(values) < period + 1:
        return None
    returns = []
    window = values[-(period + 1) :]
    for i in range(1, len(window)):
        prev = window[i - 1]
        curr = window[i]
        if math.isclose(prev, 0.0):
            continue
        returns.append((curr - prev) / prev)
    if len(returns) < 2:
        return None
    avg = sum(returns) / len(returns)
    variance = sum((r - avg) ** 2 for r in returns) / (len(returns) - 1)
    daily_std = math.sqrt(variance)
    return daily_std * math.sqrt(252)


def calculate_momentum(values: list[float], period: int = 20) -> float | None:
    if len(values) <= period or math.isclose(values[-period - 1], 0.0):
        return None
    return (values[-1] - values[-period - 1]) / values[-period - 1]


def calculate_max_drawdown(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if math.isclose(peak, 0.0):
            continue
        drawdown = (peak - value) / peak
        max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown


def calculate_rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = max(diff, 0)
        loss = abs(min(diff, 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_macd(values: list[float], short: int = 12, long: int = 26, signal_period: int = 9) -> tuple[float | None, float | None, float | None]:
    if len(values) < long + signal_period:
        return None, None, None
    ema_short = ema_series(values, short)
    ema_long = ema_series(values, long)
    macd_line = [s - l for s, l in zip(ema_short, ema_long)]
    signal_line = ema_series(macd_line, signal_period)
    macd = macd_line[-1]
    signal = signal_line[-1]
    return macd, signal, macd - signal


def calculate_kdj(values: list[float], period: int = 9) -> tuple[float | None, float | None, float | None]:
    if len(values) < period:
        return None, None, None
    k = 50.0
    d = 50.0
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        high_n = max(window)
        low_n = min(window)
        close = values[i]
        rsv = 50.0 if math.isclose(high_n, low_n) else (close - low_n) / (high_n - low_n) * 100
        k = (2 / 3) * k + (1 / 3) * rsv
        d = (2 / 3) * d + (1 / 3) * k
    j = 3 * k - 2 * d
    return k, d, j


def ema_series(values: list[float], period: int) -> list[float]:
    multiplier = 2 / (period + 1)
    ema = []
    current = values[0]
    for value in values:
        current = (value - current) * multiplier + current
        ema.append(current)
    return ema



def safe_int(raw_value: str | None, default: int) -> int:
    if raw_value is None:
        return default
    text = str(raw_value).strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return default

def safe_divide(value, denominator: float) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / denominator
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def nearest_open_trade_date(now: datetime | None = None) -> datetime.date:
    current = now or now_shanghai()
    local_today = current.astimezone(SHANGHAI_TZ).date()
    # 回退逻辑：周末自动退到最近工作日
    fallback = local_today
    while fallback.weekday() >= 5:
        fallback -= timedelta(days=1)

    if _TRADE_DATE_CACHE.get("date") == local_today and _TRADE_DATE_CACHE.get("value"):
        return _TRADE_DATE_CACHE["value"]  # type: ignore[return-value]

    try:
        import akshare as ak

        calendar_df = ak.tool_trade_date_hist_sina()
        if calendar_df is None or calendar_df.empty or "trade_date" not in calendar_df.columns:
            _TRADE_DATE_CACHE["date"] = local_today
            _TRADE_DATE_CACHE["value"] = fallback
            return fallback

        trade_dates = []
        for raw in calendar_df["trade_date"].tolist():
            dt = parse_datetime(str(raw))
            if dt:
                trade_dates.append(dt.date())
        if not trade_dates:
            _TRADE_DATE_CACHE["date"] = local_today
            _TRADE_DATE_CACHE["value"] = fallback
            return fallback
        available = [d for d in trade_dates if d <= local_today]
        resolved = max(available) if available else fallback
        _TRADE_DATE_CACHE["date"] = local_today
        _TRADE_DATE_CACHE["value"] = resolved
        return resolved
    except Exception:
        _TRADE_DATE_CACHE["date"] = local_today
        _TRADE_DATE_CACHE["value"] = fallback
        return fallback


def format_market_snapshot(snapshot: dict) -> str:
    return (
        "最新行情快照: "
        f"symbol={snapshot.get('symbol')}, "
        f"price={snapshot.get('price')}, "
        f"change_percent={snapshot.get('change_percent')}, "
        f"RSI14={snapshot.get('rsi14')}, "
        f"MACD={snapshot.get('macd')}, signal={snapshot.get('macd_signal')}, hist={snapshot.get('macd_hist')}, "
        f"KDJ(K,D,J)=({snapshot.get('kdj_k')},{snapshot.get('kdj_d')},{snapshot.get('kdj_j')}), "
        f"SMA20/SMA60=({snapshot.get('sma20')},{snapshot.get('sma60')}), EMA20={snapshot.get('ema20')}, "
        f"BOLL(upper,mid,lower)=({snapshot.get('boll_upper')},{snapshot.get('boll_mid')},{snapshot.get('boll_lower')}), "
        f"volatility20={snapshot.get('volatility20')}, momentum20={snapshot.get('momentum20')}, "
        f"max_drawdown120={snapshot.get('max_drawdown120')}, support20={snapshot.get('support20')}, resistance20={snapshot.get('resistance20')}, trend_strength={snapshot.get('trend_strength')}, "
        f"ATR14={snapshot.get('atr14')}, OBV={snapshot.get('obv')}, "
        f"timestamp={snapshot.get('timestamp')}"
    )


if __name__ == "__main__":
    run()
