#!/usr/bin/env python3
"""使用 AIHUBMIX + DuckDuckGo 生成股票投资建议（短线/长线）。"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import smtplib
import time
import textwrap
from urllib.parse import urlparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Iterable, List



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
    ]

    queries: List[str] = []
    for alias in aliases:
        for template in topic_templates:
            queries.append(template.format(alias=alias))

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
            aliases.append(f"SH{code}")
            aliases.append(f"上证{code}")
        else:
            aliases.append(f"{code}.SZ")
            aliases.append(f"SZ{code}")
            aliases.append(f"深证{code}")

    return list(dict.fromkeys(aliases))


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

    print(f"[进度] {stock_code}: 检索完成，共收集 {len(results)} 条")
    return results


def build_user_prompt(stock_code: str, contexts: List[dict]) -> str:
    context_lines = []
    for i, item in enumerate(contexts, start=1):
        context_lines.append(
            f"[{i}] query={item['query']}\\ndate={item.get('published_at', 'unknown')}\\ntitle={item['title']}\\nurl={item['href']}\\nsummary={item['body']}"
        )

    context_blob = "\n\n".join(context_lines) if context_lines else "(无检索结果)"

    return textwrap.dedent(
        f"""
        请基于以下关于股票 {stock_code} 的信息，输出可执行的投资建议。

        要求：
        0) 只允许使用最近 3 个月内的新闻，且股价/技术指标必须按“最新可得数据”解读；若无法确认最新性，直接标注“数据不足”。
        1) 必须分别给出“短线建议（1天~2周）”与“长线建议（3个月~3年）”。
        2) 每类建议都要包含：方向（买入/持有/减仓/观望）、仓位建议（百分比）、触发条件、止损/风控、关键依据。
        3) 如果信息不足或冲突，明确写出不确定性与补充观察点。
        4) 严禁保证收益，必须包含风险提示。
        5) 用简体中文，结构化输出。

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
                    "你是专业证券投研助手。你只能根据用户提供的信息做出审慎分析，"
                    "输出操作建议时必须同时给出风险管理建议。"
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

    final_results = []
    advice_by_stock: dict[str, str] = {}
    total = len(config.stock_codes)
    for index, code in enumerate(config.stock_codes, start=1):
        print(f"\n========== {code} ({index}/{total}) ==========")
        contexts = search_context(code, config.max_search_results, config.search_region)
        print(f"[进度] {code}: 开始请求 AI 生成建议")
        advice = request_ai_advice(config, code, contexts)
        print(f"[进度] {code}: AI 建议生成完成")
        print(advice)
        final_results.append(
            {
                "stock_code": code,
                "search_context_count": len(contexts),
                "advice": advice,
            }
        )
        advice_by_stock[code] = advice

    if config.email_stock_router:
        send_group_emails(config, advice_by_stock)

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


def send_group_emails(config: Config, advice_by_stock: dict[str, str]) -> None:
    if not config.sender_email or not config.sender_auth_code:
        raise ValueError("已配置 EMAIL_STOCK_ROUTER，但缺少 SENDER_EMAIL 或 SENDER_AUTH_CODE")
    if not config.smtp_host:
        raise ValueError("无法确定 SMTP_HOST，请设置环境变量 SMTP_HOST")

    with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30) as smtp:
        smtp.login(config.sender_email, config.sender_auth_code)
        for receiver, stocks in config.email_stock_router.items():
            body_lines = ["以下为今日股票分析：", ""]
            for code in stocks:
                advice = advice_by_stock.get(code)
                if not advice:
                    continue
                body_lines.extend([f"## {code}", advice, ""])

            if len(body_lines) <= 2:
                continue

            message = MIMEText("\n".join(body_lines), "plain", "utf-8")
            message["Subject"] = "股票分析日报"
            message["From"] = formataddr(("Stock Adviser", config.sender_email))
            message["To"] = receiver
            smtp.sendmail(config.sender_email, [receiver], message.as_string())
            print(f"[进度] 邮件发送完成 -> {receiver} ({len(stocks)} 只股票)")


if __name__ == "__main__":
    run()
