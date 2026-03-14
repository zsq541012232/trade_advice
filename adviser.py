#!/usr/bin/env python3
"""使用 AIHUBMIX + DuckDuckGo 生成股票投资建议（短线/长线）。"""

from __future__ import annotations

import argparse
import json
import os
import textwrap
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Iterable, List



@dataclass
class Config:
    aihubmix_api_key: str
    aihubmix_base_url: str
    aihubmix_model: str
    stock_codes: List[str]
    max_search_results: int
    search_region: str


def load_config() -> Config:
    api_key = os.getenv("AIHUBMIX_API_KEY", "").strip()
    if not api_key:
        raise ValueError("缺少环境变量 AIHUBMIX_API_KEY")

    base_url = normalize_base_url(os.getenv("AIHUBMIX_BASE_URL", "https://api.aihubmix.com/v1"))
    model = os.getenv("AIHUBMIX_MODEL", "gpt-4o-mini").strip()

    raw_codes = os.getenv("STOCK_CODES", "").strip()
    if not raw_codes:
        raise ValueError("缺少环境变量 STOCK_CODES，例如：AAPL,TSLA,600519.SS")

    stock_codes = [code.strip() for code in raw_codes.split(",") if code.strip()]
    if not stock_codes:
        raise ValueError("STOCK_CODES 解析后为空，请检查格式")

    raw_max_results = os.getenv("DUCKDUCKGO_MAX_RESULTS", "5").strip()
    if not raw_max_results:
        raw_max_results = "5"

    try:
        max_search_results = int(raw_max_results)
    except ValueError as exc:
        raise ValueError("环境变量 DUCKDUCKGO_MAX_RESULTS 必须是整数") from exc

    if max_search_results <= 0:
        raise ValueError("环境变量 DUCKDUCKGO_MAX_RESULTS 必须大于 0")

    search_region = os.getenv("DUCKDUCKGO_REGION", "cn-zh")

    return Config(
        aihubmix_api_key=api_key,
        aihubmix_base_url=base_url.rstrip("/"),
        aihubmix_model=model,
        stock_codes=stock_codes,
        max_search_results=max_search_results,
        search_region=search_region,
    )


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
        "{alias} 新闻 舆情 最新",
        "{alias} 财报 业绩 指引",
        "{alias} 股价 技术指标 成交量 RSI MACD",
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


def search_context(stock_code: str, max_results: int, region: str) -> List[dict]:
    results: List[dict] = []
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    queries = build_queries(stock_code)
    print(f"[进度] {stock_code}: 开始检索，共 {len(queries)} 条查询，区域={region}")

    with DDGS() as ddgs:
        for idx, query in enumerate(queries, start=1):
            print(f"[进度] {stock_code}: 检索 {idx}/{len(queries)} -> {query}")
            hits: Iterable[dict] = ddgs.text(
                query,
                region=region,
                max_results=max_results,
                safesearch="off",
            )
            query_count = 0
            for hit in hits:
                query_count += 1
                results.append(
                    {
                        "query": query,
                        "title": hit.get("title", ""),
                        "href": hit.get("href", ""),
                        "body": hit.get("body", ""),
                    }
                )
            print(f"[进度] {stock_code}: 该查询命中 {query_count} 条")

    if not results:
        print(f"[进度] {stock_code}: 主区域无结果，尝试使用全球区域兜底（wt-wt）")
        with DDGS() as ddgs:
            for query in queries:
                hits: Iterable[dict] = ddgs.text(
                    query,
                    region="wt-wt",
                    max_results=max_results,
                    safesearch="off",
                )
                for hit in hits:
                    results.append(
                        {
                            "query": query,
                            "title": hit.get("title", ""),
                            "href": hit.get("href", ""),
                            "body": hit.get("body", ""),
                        }
                    )

    print(f"[进度] {stock_code}: 检索完成，共收集 {len(results)} 条")
    return results


def build_user_prompt(stock_code: str, contexts: List[dict]) -> str:
    context_lines = []
    for i, item in enumerate(contexts, start=1):
        context_lines.append(
            f"[{i}] query={item['query']}\\ntitle={item['title']}\\nurl={item['href']}\\nsummary={item['body']}"
        )

    context_blob = "\n\n".join(context_lines) if context_lines else "(无检索结果)"

    return textwrap.dedent(
        f"""
        请基于以下关于股票 {stock_code} 的信息，输出可执行的投资建议。

        要求：
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

    if args.pretty_json:
        print("\n========== JSON ==========")
        print(json.dumps(final_results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
