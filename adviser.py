#!/usr/bin/env python3
"""使用 AIHUBMIX + DuckDuckGo 生成股票投资建议（短线/长线）。"""

from __future__ import annotations

import argparse
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


_TRADE_DATE_CACHE: dict[str, object] = {"date": None, "value": None}



@dataclass
class Config:
    aihubmix_api_key: str
    aihubmix_base_url: str
    aihubmix_model: str
    stock_codes: List[str]
    max_search_results: int
    search_region: str
    email_stock_router: dict[str, List[str]] = field(default_factory=dict)
    sender_email: str | None = None
    sender_auth_code: str | None = None
    smtp_host: str = ""
    smtp_port: int = 465
    market_data_provider: str = "auto"


@dataclass
class StockResearchResult:
    stock_code: str
    contexts: List[dict]
    advice: str


def load_config() -> Config:
    api_key = os.getenv("AIHUBMIX_API_KEY", "").strip()
    if not api_key:
        raise ValueError("缺少环境变量 AIHUBMIX_API_KEY")

    base_url = normalize_base_url(os.getenv("AIHUBMIX_BASE_URL", "https://api.aihubmix.com/v1"))
    model = os.getenv("AIHUBMIX_MODEL", "gpt-4o-mini").strip()

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

    market_data_provider = os.getenv("MARKET_DATA_PROVIDER", "auto").strip().lower() or "auto"
    if market_data_provider not in {"auto", "akshare", "yahoo", "eastmoney", "sina", "tencent", "stooq"}:
        raise ValueError(
            "环境变量 MARKET_DATA_PROVIDER 仅支持 auto / akshare / yahoo / eastmoney / sina / tencent / stooq"
        )

    return Config(
        aihubmix_api_key=api_key,
        aihubmix_base_url=base_url.rstrip("/"),
        aihubmix_model=model,
        stock_codes=stock_codes,
        max_search_results=max_search_results,
        search_region=search_region,
        email_stock_router=router,
        sender_email=sender_email,
        sender_auth_code=sender_auth_code,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        market_data_provider=market_data_provider,
    )


def parse_email_stock_router(raw_router: str) -> dict[str, List[str]]:
    """解析 EMAIL_STOCK_ROUTER：a@example.com:AAPL,TSLA;b@example.com:MSFT。"""
    router: dict[str, List[str]] = {}
    value = raw_router.strip()
    if not value:
        return router

    for item in value.split(";"):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(
                "EMAIL_STOCK_ROUTER 格式错误，示例：a@x.com:AAPL,TSLA;b@y.com:MSFT"
            )
        email, stocks_blob = item.split(":", 1)
        receiver = email.strip()
        stocks = [code.strip().upper() for code in stocks_blob.split(",") if code.strip()]
        if not receiver or not stocks:
            raise ValueError(
                "EMAIL_STOCK_ROUTER 格式错误，示例：a@x.com:AAPL,TSLA;b@y.com:MSFT"
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


def normalize_base_url(raw_base_url: str) -> str:
    base_url = raw_base_url.strip()
    if not base_url:
        return "https://api.aihubmix.com/v1"

    parsed = urlparse(base_url)

    if parsed.scheme and parsed.netloc:
        return base_url.rstrip("/")

    if not parsed.scheme and parsed.path and not parsed.path.startswith("/"):
        with_scheme = f"https://{parsed.path}"
        parsed_with_scheme = urlparse(with_scheme)
        if parsed_with_scheme.netloc:
            return with_scheme.rstrip("/")

    raise ValueError(
        "环境变量 AIHUBMIX_BASE_URL 无效，请提供完整 URL（例如 https://api.aihubmix.com/v1）"
    )


def build_queries(stock_code: str) -> List[str]:
    aliases = stock_code_aliases(stock_code)
    topic_templates = [
        "{alias} 股票 最新消息",
        "{alias} 财报 业绩 指引",
        "{alias} 股价 分析 技术指标",
        "{alias} 银行业 宏观政策 影响",
    ]

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
                print(f"[进度] 检索异常（query={query}, region={region}）：{exc}")
                return []
            time.sleep(2 ** (attempt - 1))

    return []


def search_context(stock_code: str, max_results: int, region: str) -> List[dict]:
    results: List[dict] = []
    from ddgs import DDGS

    queries = build_queries(stock_code)
    fallback_regions = list(dict.fromkeys([region or "zh-cn", "zh-cn", "wt-wt"]))
    print(
        f"[进度] {stock_code}: 开始检索，共 {len(queries)} 条查询，"
        f"区域策略={','.join(fallback_regions)}"
    )

    seen_urls = set()
    for current_region in fallback_regions:
        with DDGS() as ddgs:
            for idx, query in enumerate(queries, start=1):
                print(
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
                print(f"[进度] {stock_code}: 该查询命中 {query_count} 条")

        if results:
            break

    if len(results) < max_results:
        print(f"[进度] {stock_code}: DuckDuckGo 结果偏少，尝试 Google/Bing News RSS 兜底")
        rss_results = search_context_via_rss(stock_code, queries, max_results=max_results * 2)
        for hit in rss_results:
            href = hit.get("href", "")
            if href and href in seen_urls:
                continue
            if href:
                seen_urls.add(href)
            results.append(hit)

    print(f"[进度] {stock_code}: 检索完成，共收集 {len(results)} 条")
    return results


def search_context_via_rss(stock_code: str, queries: list[str], max_results: int) -> list[dict]:
    import requests

    endpoints = [
        ("google-news-rss", "https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
        ("bing-news-rss", "https://www.bing.com/news/search?q={query}&format=rss&setlang=zh-cn"),
    ]
    collected: list[dict] = []
    for query in queries:
        for source_name, template in endpoints:
            url = template.format(query=quote_plus(query))
            try:
                resp = requests.get(url, timeout=20)
                resp.raise_for_status()
                items = parse_rss_items(resp.text)
            except Exception as exc:
                print(f"[进度] {stock_code}: RSS 检索失败（{source_name}）: {exc}")
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


def request_ai_advice(config: Config, stock_code: str, contexts: List[dict]) -> str:
    url = f"{config.aihubmix_base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.aihubmix_api_key}",
        "Content-Type": "application/json",
    }

    import requests

    payload = {
        "model": resolve_model(config, requests),
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

    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = json.dumps(resp.json(), ensure_ascii=False)
        except ValueError:
            detail = resp.text.strip()
        raise RuntimeError(
            f"AIHUBMIX 请求失败（HTTP {resp.status_code}）。"
            f"请检查 AIHUBMIX_MODEL 是否可用，当前值：{payload['model']}。"
            f"响应：{detail}"
        ) from exc
    data = resp.json()

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"AIHUBMIX 返回格式异常: {json.dumps(data, ensure_ascii=False)}") from exc


def resolve_model(config: Config, requests_module) -> str:
    """优先使用用户配置模型，不可用时给出清晰报错与可选模型。"""
    url = f"{config.aihubmix_base_url}/models"
    headers = {"Authorization": f"Bearer {config.aihubmix_api_key}"}

    try:
        resp = requests_module.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        # /models 不可用时，不阻断主流程，回退到用户指定模型。
        return config.aihubmix_model

    model_ids = {
        item.get("id", "")
        for item in data.get("data", [])
        if isinstance(item, dict) and item.get("id")
    }

    if not model_ids or config.aihubmix_model in model_ids:
        return config.aihubmix_model

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
    parser = argparse.ArgumentParser(description="股票投资建议生成器（AIHUBMIX + DuckDuckGo）")
    parser.add_argument(
        "--pretty-json",
        action="store_true",
        help="额外输出结构化 JSON（包含检索信息与大模型回复）",
    )
    args = parser.parse_args()

    config = load_config()
    print(f"[进度] 已加载配置，共 {len(config.stock_codes)} 只股票待分析")

    final_results: list[dict] = []
    research_cache: dict[str, StockResearchResult] = {}
    total = len(config.stock_codes)
    for index, code in enumerate(config.stock_codes, start=1):
        print(f"\n========== {code} ({index}/{total}) ==========")
        market_snapshot = fetch_market_snapshot(code, config)
        if market_snapshot:
            print(f"[进度] {code}: 行情源={market_snapshot.get('provider')} 时间={market_snapshot.get('timestamp')}")
        contexts = search_context(code, config.max_search_results, config.search_region)
        if market_snapshot:
            contexts.insert(0, {
                "query": f"{code} 实时行情技术指标",
                "region": "direct-api",
                "title": f"{code} 最新价格与技术指标（{market_snapshot.get('provider')}）",
                "href": market_snapshot.get("source_url", ""),
                "body": format_market_snapshot(market_snapshot),
                "published_at": market_snapshot.get("date", "unknown"),
            })
        print(f"[进度] {code}: 开始请求 AI 生成建议")
        advice = request_ai_advice(config, code, contexts)
        print(f"[进度] {code}: AI 建议生成完成")
        print(advice)
        research_cache[code] = StockResearchResult(
            stock_code=code,
            contexts=contexts,
            advice=advice,
        )
        final_results.append(build_result_dict(research_cache[code]))

    if config.email_stock_router:
        send_group_emails(config, research_cache)

    if args.pretty_json:
        print("\n========== JSON ==========")
        print(json.dumps(final_results, ensure_ascii=False, indent=2))


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
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=92)
    return dt >= cutoff


def send_group_emails(config: Config, research_by_stock: dict[str, StockResearchResult]) -> None:
    if not config.sender_email or not config.sender_auth_code:
        raise ValueError("已配置 EMAIL_STOCK_ROUTER，但缺少 SENDER_EMAIL 或 SENDER_AUTH_CODE")
    if not config.smtp_host:
        raise ValueError("无法确定 SMTP_HOST，请设置环境变量 SMTP_HOST")

    with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30) as smtp:
        smtp.login(config.sender_email, config.sender_auth_code)
        for receiver, stocks in config.email_stock_router.items():
            html_sections: list[str] = []
            plain_lines = ["以下为今日股票分析：", ""]
            for code in stocks:
                research = research_by_stock.get(code)
                if not research:
                    continue
                advice = research.advice
                safe_advice = markdown_to_html(advice)
                html_sections.append(
                    "<section style='margin:14px 0;padding:12px;border:1px solid #e5e7eb;border-radius:10px;'>"
                    f"<h3 style='margin:0 0 8px 0;color:#111827;'>{code}</h3>"
                    f"<div style='line-height:1.65;color:#1f2937;font-size:14px;'>{safe_advice}</div>"
                    "</section>"
                )
                plain_lines.extend([f"## {code}", advice, ""])

            if not html_sections:
                continue

            summary_rows = "".join(
                "<tr>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #eef2f7;'>{code}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #eef2f7;'>{len(research_by_stock.get(code, StockResearchResult(code, [], '')).contexts)}</td>"
                "<td style='padding:8px 10px;border-bottom:1px solid #eef2f7;'>🧠 AI 已生成</td>"
                "</tr>"
                for code in stocks
                if code in research_by_stock
            )

            html_body = (
                "<html><body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f8fafc;padding:14px;'>"
                "<div style='max-width:860px;margin:0 auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;padding:16px;'>"
                "<h2 style='margin:0 0 8px 0;color:#111827;'>📈 股票分析日报</h2>"
                "<p style='margin:0 0 12px 0;color:#6b7280;'>"
                f"⏰ 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ｜ 📬 接收人：{receiver}"
                "</p>"
                "<table style='width:100%;border-collapse:collapse;margin:0 0 12px 0;font-size:13px;'>"
                "<thead><tr style='background:#f1f5f9;color:#334155;'>"
                "<th style='text-align:left;padding:8px 10px;'>股票</th>"
                "<th style='text-align:left;padding:8px 10px;'>样本条数</th>"
                "<th style='text-align:left;padding:8px 10px;'>状态</th>"
                "</tr></thead>"
                f"<tbody>{summary_rows}</tbody></table>"
                "<p style='margin:0 0 10px 0;'>✅ 建议阅读顺序：先看“核心结论”→再看“执行计划”→最后核对“风险提示”。</p>"
                + "".join(html_sections)
                + "<p style='margin-top:16px;color:#6b7280;font-size:12px;'>"
                "⚠️ 风险提示：以上内容仅供参考，不构成任何投资建议，请严格做好仓位与止损管理。"
                "</p></div></body></html>"
            )

            message = MIMEMultipart("alternative")
            message.attach(MIMEText("\n".join(plain_lines), "plain", "utf-8"))
            message.attach(MIMEText(html_body, "html", "utf-8"))
            message["Subject"] = "股票分析日报"
            message["From"] = formataddr(("Stock Adviser", config.sender_email))
            message["To"] = receiver
            smtp.sendmail(config.sender_email, [receiver], message.as_string())
            print(f"[进度] 邮件发送完成 -> {receiver} ({len(stocks)} 只股票)")


def build_result_dict(research: StockResearchResult) -> dict:
    return {
        "stock_code": research.stock_code,
        "search_context_count": len(research.contexts),
        "advice": research.advice,
    }


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
            print(f"[进度] {stock_code}: {provider} 行情抓取失败: {exc}")
            snapshot = None

        if snapshot:
            return snapshot
    return None


def fetch_market_snapshot_from_yahoo(stock_code: str) -> dict | None:
    import requests

    symbol = to_yahoo_symbol(stock_code)
    target_trade_date = nearest_open_trade_date()
    quote_url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}"
    quote_resp = requests.get(quote_url, timeout=20)
    quote_resp.raise_for_status()
    quote_results = quote_resp.json().get("quoteResponse", {}).get("result", [])
    if not quote_results:
        return None
    quote = quote_results[0]

    chart_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=6mo&interval=1d"
    chart_resp = requests.get(chart_url, timeout=20)
    chart_resp.raise_for_status()
    chart_result = chart_resp.json().get("chart", {}).get("result", [])
    if not chart_result:
        return None

    chart = chart_result[0]
    timestamps = chart.get("timestamp", [])
    raw_closes = chart.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    closes: list[float] = []
    selected_price = None
    selected_timestamp = None
    for ts, close in zip(timestamps, raw_closes):
        if not isinstance(close, (int, float)):
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        if dt.date() <= target_trade_date:
            closes.append(float(close))
            selected_price = float(close)
            selected_timestamp = dt.isoformat()

    if not closes:
        closes = [v for v in raw_closes if isinstance(v, (int, float))]
    indicators = calculate_indicators(closes)

    return {
        "provider": "yahoo",
        "symbol": symbol,
        "price": selected_price or quote.get("regularMarketPrice"),
        "change_percent": quote.get("regularMarketChangePercent"),
        "timestamp": selected_timestamp or quote.get("regularMarketTime"),
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
        print(f"[进度] {stock_code}: akshare 历史行情重试后仍失败: {last_exception}")
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
    for value in df["收盘"].tolist():
        parsed = safe_float(value)
        if parsed is not None:
            closes.append(parsed)
    if not closes:
        return None

    last_row = df.iloc[-1]
    indicators = calculate_indicators(closes)
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
    resp = requests.get(url, timeout=20)
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

    history = fetch_recent_closes_from_eastmoney(stock_code)
    indicators = calculate_indicators(history)
    return {
        "provider": "sina",
        "symbol": symbol,
        "price": price,
        "change_percent": change_percent,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "date": datetime.now(timezone.utc).date().isoformat(),
        "source_url": f"https://finance.sina.com.cn/realstock/company/{exchange}{symbol}/nc.shtml",
        **indicators,
    }


def fetch_market_snapshot_from_tencent(stock_code: str) -> dict | None:
    import requests

    secid, symbol = to_eastmoney_secid(stock_code)
    exchange = "sh" if secid.startswith("1.") else "sz"
    url = f"https://qt.gtimg.cn/q={exchange}{symbol}"
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    parts = resp.text.split("~")
    if len(parts) < 40:
        return None
    price = safe_float(parts[3])
    change_percent = safe_float(parts[32])
    history = fetch_recent_closes_from_eastmoney(stock_code)
    indicators = calculate_indicators(history)
    return {
        "provider": "tencent",
        "symbol": symbol,
        "price": price,
        "change_percent": change_percent,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "date": datetime.now(timezone.utc).date().isoformat(),
        "source_url": f"https://gu.qq.com/{exchange}{symbol}",
        **indicators,
    }


def fetch_market_snapshot_from_stooq(stock_code: str) -> dict | None:
    import requests

    symbol = to_yahoo_symbol(stock_code).replace(".SS", ".CN").replace(".SZ", ".CN")
    url = f"https://stooq.com/q/d/l/?s={symbol.lower()}&i=d"
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    lines = [line.strip() for line in resp.text.splitlines() if line.strip()]
    if len(lines) < 3:
        return None
    closes: list[float] = []
    last_close = None
    last_date = None
    for row in lines[1:]:
        cols = row.split(",")
        if len(cols) < 5:
            continue
        close = safe_float(cols[4])
        if close is None:
            continue
        closes.append(close)
        last_close = close
        last_date = cols[0]
    if not closes:
        return None
    indicators = calculate_indicators(closes)
    return {
        "provider": "stooq",
        "symbol": symbol,
        "price": last_close,
        "change_percent": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "date": last_date or datetime.now(timezone.utc).date().isoformat(),
        "source_url": f"https://stooq.com/q/?s={symbol.lower()}",
        **indicators,
    }


def fetch_recent_closes_from_eastmoney(stock_code: str) -> list[float]:
    import requests

    secid, _ = to_eastmoney_secid(stock_code)
    kline_url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
        "&klt=101&fqt=1&lmt=180"
    )
    kline_resp = requests.get(kline_url, timeout=20)
    kline_resp.raise_for_status()
    klines = kline_resp.json().get("data", {}).get("klines", [])

    closes: list[float] = []
    for row in klines:
        parts = row.split(",")
        if len(parts) < 3:
            continue
        close = safe_float(parts[2])
        if close is not None:
            closes.append(close)
    return closes


def fetch_market_snapshot_from_eastmoney(stock_code: str) -> dict | None:
    import requests

    secid, symbol = to_eastmoney_secid(stock_code)
    quote_url = (
        "https://push2.eastmoney.com/api/qt/stock/get?"
        f"secid={secid}&fields=f43,f44,f45,f46,f47,f48,f49,f57,f58,f60,f169,f170"
    )
    quote_resp = requests.get(quote_url, timeout=20)
    quote_resp.raise_for_status()
    data = quote_resp.json().get("data")
    if not data:
        return None

    kline_url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
        "&klt=101&fqt=1&lmt=180"
    )
    kline_resp = requests.get(kline_url, timeout=20)
    kline_resp.raise_for_status()
    klines = kline_resp.json().get("data", {}).get("klines", [])

    closes = fetch_recent_closes_from_eastmoney(stock_code)
    indicators = calculate_indicators(closes)
    price = safe_divide(data.get("f43"), 100)
    change_percent = safe_divide(data.get("f170"), 100)

    return {
        "provider": "eastmoney",
        "symbol": symbol,
        "price": price,
        "change_percent": change_percent,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "date": datetime.now(timezone.utc).date().isoformat(),
        "source_url": f"https://quote.eastmoney.com/{symbol}.html",
        **indicators,
    }


def to_yahoo_symbol(stock_code: str) -> str:
    code = stock_code.strip().upper()
    if "." in code:
        return code
    if code.isdigit() and len(code) == 6:
        suffix = "SS" if code.startswith("6") else "SZ"
        return f"{code}.{suffix}"
    return code


def to_eastmoney_secid(stock_code: str) -> tuple[str, str]:
    code = stock_code.strip().upper()
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


def calculate_indicators(closes: list[float]) -> dict:
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
    }


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
    current = now or datetime.now(timezone.utc)
    local_today = (current + timedelta(hours=8)).date()
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
        f"timestamp={snapshot.get('timestamp')}"
    )


if __name__ == "__main__":
    run()
