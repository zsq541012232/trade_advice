#!/usr/bin/env python3
"""使用 AIHUBMIX + DuckDuckGo 生成股票投资建议（短线/长线）。"""

from __future__ import annotations

import argparse
import json
import os
import textwrap
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

    base_url = os.getenv("AIHUBMIX_BASE_URL", "https://api.aihubmix.com/v1").strip()
    model = os.getenv("AIHUBMIX_MODEL", "gpt-4o-mini").strip()

    raw_codes = os.getenv("STOCK_CODES", "").strip()
    if not raw_codes:
        raise ValueError("缺少环境变量 STOCK_CODES，例如：AAPL,TSLA,600519.SS")

    stock_codes = [code.strip() for code in raw_codes.split(",") if code.strip()]
    if not stock_codes:
        raise ValueError("STOCK_CODES 解析后为空，请检查格式")

    max_search_results = int(os.getenv("DUCKDUCKGO_MAX_RESULTS", "5"))
    search_region = os.getenv("DUCKDUCKGO_REGION", "cn-zh")

    return Config(
        aihubmix_api_key=api_key,
        aihubmix_base_url=base_url.rstrip("/"),
        aihubmix_model=model,
        stock_codes=stock_codes,
        max_search_results=max_search_results,
        search_region=search_region,
    )


def build_queries(stock_code: str) -> List[str]:
    return [
        f"{stock_code} 新闻 舆情 最新",
        f"{stock_code} 财报 业绩 指引",
        f"{stock_code} 股价 技术指标 成交量 RSI MACD",
    ]


def search_context(stock_code: str, max_results: int, region: str) -> List[dict]:
    results: List[dict] = []
    from duckduckgo_search import DDGS

    with DDGS() as ddgs:
        for query in build_queries(stock_code):
            hits: Iterable[dict] = ddgs.text(
                query,
                region=region,
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

    payload = {
        "model": config.aihubmix_model,
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

    import requests

    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"AIHUBMIX 返回格式异常: {json.dumps(data, ensure_ascii=False)}") from exc


def run() -> None:
    parser = argparse.ArgumentParser(description="股票投资建议生成器（AIHUBMIX + DuckDuckGo）")
    parser.add_argument(
        "--pretty-json",
        action="store_true",
        help="额外输出结构化 JSON（包含检索信息与大模型回复）",
    )
    args = parser.parse_args()

    config = load_config()

    final_results = []
    for code in config.stock_codes:
        print(f"\n========== {code} ==========")
        contexts = search_context(code, config.max_search_results, config.search_region)
        advice = request_ai_advice(config, code, contexts)
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
