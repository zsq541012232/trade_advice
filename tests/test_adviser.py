import adviser


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "raw error"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception("http error")

    def json(self):
        return self._payload


class _FakeRequests:
    def __init__(self, response):
        self._response = response

    def get(self, *args, **kwargs):
        return self._response


def test_build_queries_contains_expected_sections():
    queries = adviser.build_queries("AAPL")
    assert len(queries) == 5
    assert "最新消息" in queries[0]
    assert "财报" in queries[1]
    assert "技术指标" in queries[2]


def test_build_queries_accepts_adaptive_topics():
    queries = adviser.build_queries("AAPL", adaptive_topics=["半导体", "AI", "AI", " "])
    assert len(queries) == 7
    assert "AAPL 半导体 行业政策 影响" in queries
    assert "AAPL AI 行业政策 影响" in queries


def test_build_user_prompt_contains_short_and_long_term_requirements():
    contexts = [
        {
            "query": "AAPL 新闻 舆情 最新",
            "title": "Apple 新闻",
            "href": "https://example.com/news",
            "body": "示例摘要",
        }
    ]
    prompt = adviser.build_user_prompt("AAPL", contexts)
    assert "决策仪表盘" in prompt
    assert "分析结果摘要" in prompt
    assert "作战计划" in prompt
    assert "报告生成时间" in prompt
    assert "AAPL" in prompt
    assert "https://example.com/news" in prompt


def test_load_config_reads_env_vars(monkeypatch):
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("STOCK_CODES", "AAPL,TSLA")
    monkeypatch.setenv("AIHUBMIX_BASE_URL", "https://api.aihubmix.com/v1/")
    monkeypatch.setenv("AIHUBMIX_MODEL", "gpt-test")
    monkeypatch.setenv("DUCKDUCKGO_MAX_RESULTS", "7")
    monkeypatch.setenv("DUCKDUCKGO_REGION", "us-en")

    config = adviser.load_config()

    assert config.aihubmix_api_key == "test-key"
    assert config.stock_codes == ["AAPL", "TSLA"]
    assert config.aihubmix_base_url == "https://api.aihubmix.com/v1"
    assert config.aihubmix_model == "gpt-test"
    assert config.max_search_results == 7
    assert config.search_region == "us-en"


def test_load_config_accepts_extended_market_data_provider(monkeypatch):
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("STOCK_CODES", "AAPL")
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "tencent")

    config = adviser.load_config()

    assert config.market_data_provider == "tencent"


def test_load_config_uses_default_when_max_results_is_empty(monkeypatch):
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("STOCK_CODES", "AAPL")
    monkeypatch.setenv("DUCKDUCKGO_MAX_RESULTS", "")

    config = adviser.load_config()

    assert config.max_search_results == 5


def test_load_config_raises_when_max_results_is_not_int(monkeypatch):
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("STOCK_CODES", "AAPL")
    monkeypatch.setenv("DUCKDUCKGO_MAX_RESULTS", "abc")

    try:
        adviser.load_config()
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "DUCKDUCKGO_MAX_RESULTS 必须是整数" in str(exc)


def test_load_config_raises_when_max_results_is_not_positive(monkeypatch):
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("STOCK_CODES", "AAPL")
    monkeypatch.setenv("DUCKDUCKGO_MAX_RESULTS", "0")

    try:
        adviser.load_config()
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "DUCKDUCKGO_MAX_RESULTS 必须大于 0" in str(exc)


def test_load_config_accepts_base_url_without_scheme(monkeypatch):
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("STOCK_CODES", "AAPL")
    monkeypatch.setenv("AIHUBMIX_BASE_URL", "api.aihubmix.com/v1")

    config = adviser.load_config()

    assert config.aihubmix_base_url == "https://api.aihubmix.com/v1"


def test_load_config_raises_when_base_url_is_invalid(monkeypatch):
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("STOCK_CODES", "AAPL")
    monkeypatch.setenv("AIHUBMIX_BASE_URL", "/chat/completions")

    try:
        adviser.load_config()
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "AIHUBMIX_BASE_URL 无效" in str(exc)


def test_resolve_model_keeps_configured_model_when_available():
    config = adviser.Config(
        llm_provider="aihubmix",
        aihubmix_api_key="k",
        aihubmix_base_url="https://api.aihubmix.com/v1",
        aihubmix_model="gpt-4o-mini",
        stock_codes=["AAPL"],
        max_search_results=5,
        search_region="zh-cn",
    )
    req = _FakeRequests(_FakeResponse({"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]}))

    model = adviser.resolve_model(config, req)

    assert model == "gpt-4o-mini"


def test_resolve_model_falls_back_to_preferred_candidate_when_missing():
    config = adviser.Config(
        llm_provider="aihubmix",
        aihubmix_api_key="k",
        aihubmix_base_url="https://api.aihubmix.com/v1",
        aihubmix_model="gpt-4o-mini",
        stock_codes=["AAPL"],
        max_search_results=5,
        search_region="zh-cn",
    )
    req = _FakeRequests(_FakeResponse({"data": [{"id": "deepseek-v3"}, {"id": "qwen-plus"}]}))

    model = adviser.resolve_model(config, req)

    assert model == "deepseek-v3"




def test_resolve_model_for_nim_prefers_deepseek_r1_when_available():
    config = adviser.Config(
        llm_provider="nim",
        aihubmix_api_key="",
        aihubmix_base_url="https://api.aihubmix.com/v1",
        aihubmix_model="gpt-4o-mini",
        nim_api_key="nim-k",
        nim_base_url="https://integrate.api.nvidia.com/v1",
        nim_model="unknown-nim-model",
        stock_codes=["AAPL"],
        max_search_results=5,
        search_region="zh-cn",
    )
    req = _FakeRequests(_FakeResponse({"data": [{"id": "meta/llama-3.1-8b-instruct"}, {"id": "deepseek-ai/deepseek-r1"}]}))

    model = adviser.resolve_model(config, req)

    assert model == "deepseek-ai/deepseek-r1"


def test_resolve_model_for_nim_prefers_nim_candidates_when_configured_missing():
    config = adviser.Config(
        llm_provider="nim",
        aihubmix_api_key="",
        aihubmix_base_url="https://api.aihubmix.com/v1",
        aihubmix_model="gpt-4o-mini",
        nim_api_key="nim-k",
        nim_base_url="https://integrate.api.nvidia.com/v1",
        nim_model="unknown-nim-model",
        stock_codes=["AAPL"],
        max_search_results=5,
        search_region="zh-cn",
    )
    req = _FakeRequests(_FakeResponse({"data": [{"id": "gpt-4o"}, {"id": "meta/llama-3.1-8b-instruct"}]}))

    model = adviser.resolve_model(config, req)

    assert model == "meta/llama-3.1-8b-instruct"


def test_resolve_model_for_nim_falls_back_to_first_available_when_no_nim_candidate():
    config = adviser.Config(
        llm_provider="nim",
        aihubmix_api_key="",
        aihubmix_base_url="https://api.aihubmix.com/v1",
        aihubmix_model="gpt-4o-mini",
        nim_api_key="nim-k",
        nim_base_url="https://integrate.api.nvidia.com/v1",
        nim_model="unknown-nim-model",
        stock_codes=["AAPL"],
        max_search_results=5,
        search_region="zh-cn",
    )
    req = _FakeRequests(_FakeResponse({"data": [{"id": "gpt-4o"}, {"id": "qwen-plus"}]}))

    model = adviser.resolve_model(config, req)

    assert model == "gpt-4o"
def test_stock_code_aliases_for_shanghai_code():
    aliases = adviser.stock_code_aliases("600900")
    assert aliases == ["600900", "600900.SH", "上证600900"]


def test_build_queries_expands_aliases_for_a_share_code():
    queries = adviser.build_queries("600900")
    assert len(queries) == 10
    assert "600900 股票 最新消息" in queries
    assert "600900.SH 财报 业绩 指引" in queries
    assert "600900 股价 分析 技术指标" in queries


def test_to_yahoo_symbol_accepts_common_us_code_formats():
    assert adviser.to_yahoo_symbol("AAPL") == "AAPL"
    assert adviser.to_yahoo_symbol("US.AAPL") == "AAPL"
    assert adviser.to_yahoo_symbol("AAPL.US") == "AAPL"


def test_to_eastmoney_secid_accepts_prefixed_a_share_code():
    secid, symbol = adviser.to_eastmoney_secid("SH.600900")
    assert secid == "1.600900"
    assert symbol == "600900"


def test_normalize_query_key_dedup_semantic_aliases():
    q1 = adviser.normalize_query_key("600900 股票 最新消息")
    q2 = adviser.normalize_query_key("SH600900 股票 最新消息")
    assert q1 == q2


def test_nearest_open_trade_date_weekend_fallback():
    from datetime import datetime, timezone

    sunday = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
    d = adviser.nearest_open_trade_date(sunday)
    # 至少应回退到工作日
    assert d.weekday() < 5


def test_parse_email_stock_router():
    router = adviser.parse_email_stock_router("a@test.com:AAPL,TSLA;b@test.com:MSFT")
    assert router == {
        "a@test.com": ["AAPL", "TSLA"],
        "b@test.com": ["MSFT"],
    }


def test_parse_email_stock_router_accepts_newline_separator():
    router = adviser.parse_email_stock_router("a@test.com:AAPL,TSLA\nb@test.com:MSFT")
    assert router == {
        "a@test.com": ["AAPL", "TSLA"],
        "b@test.com": ["MSFT"],
    }


def test_calculate_indicators_includes_atr_and_obv():
    closes = [10 + i * 0.1 for i in range(100)]
    highs = [v + 0.2 for v in closes]
    lows = [v - 0.2 for v in closes]
    volumes = [1000 + i * 10 for i in range(100)]

    indicators = adviser.calculate_indicators(closes, highs=highs, lows=lows, volumes=volumes)

    assert indicators["atr14"] is not None
    assert indicators["obv"] is not None


def test_load_config_reads_chain_depth_and_email_protocol(monkeypatch):
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("STOCK_CODES", "AAPL")
    monkeypatch.setenv("CHAIN_OF_SEARCH_DEPTH", "2")
    monkeypatch.setenv("EMAIL_DELIVERY_PROTOCOL", "imap")

    config = adviser.load_config()

    assert config.chain_of_search_depth == 2
    assert config.email_delivery_protocol == "imap"


def test_parse_email_stock_router_raises_on_bad_format():
    try:
        adviser.parse_email_stock_router("a@test.com,AAPL")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "EMAIL_STOCK_ROUTER 格式错误" in str(exc)


def test_load_config_can_read_stocks_from_router(monkeypatch):
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.delenv("STOCK_CODES", raising=False)
    monkeypatch.setenv("EMAIL_STOCK_ROUTER", "a@test.com:TSLA,AAPL;b@test.com:MSFT")

    config = adviser.load_config()

    assert config.stock_codes == ["AAPL", "MSFT", "TSLA"]
    assert config.email_stock_router["a@test.com"] == ["TSLA", "AAPL"]


def test_parse_datetime_supports_cn_date():
    dt = adviser.parse_datetime("发布时间 2026年03月01日")
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 3
    assert dt.day == 1


def test_within_last_3_months():
    from datetime import datetime, timedelta, timezone

    recent = datetime.now(timezone.utc) - timedelta(days=10)
    old = datetime.now(timezone.utc) - timedelta(days=150)
    assert adviser.within_last_3_months(recent) is True
    assert adviser.within_last_3_months(old) is False


def test_to_yahoo_symbol_for_a_share():
    assert adviser.to_yahoo_symbol("600900") == "600900.SS"
    assert adviser.to_yahoo_symbol("000001") == "000001.SZ"


def test_to_eastmoney_secid_for_a_share():
    assert adviser.to_eastmoney_secid("600900") == ("1.600900", "600900")
    assert adviser.to_eastmoney_secid("000001") == ("0.000001", "000001")


def test_calculate_indicators_returns_values_for_enough_data():
    closes = [10 + i * 0.1 for i in range(100)]
    indicators = adviser.calculate_indicators(closes)
    assert indicators["rsi14"] is not None
    assert indicators["macd"] is not None
    assert indicators["kdj_k"] is not None
    assert indicators["boll_upper"] is not None
    assert indicators["volatility20"] is not None
    assert indicators["max_drawdown120"] is not None


def test_calculate_indicators_returns_none_for_short_series():
    indicators = adviser.calculate_indicators([1.0, 1.1, 1.2])
    assert indicators["rsi14"] is None
    assert indicators["macd"] is None
    assert indicators["kdj_k"] is None
    assert indicators["boll_upper"] is None
    assert indicators["trend_strength"] is None


def test_markdown_to_html_supports_headings_lists_and_links():
    markdown = """## 标题

- **买入** 条件
- 关注 [公告](https://example.com)

`代码块`"""
    html = adviser.markdown_to_html(markdown)
    assert "<h3" in html
    assert "<ul" in html and "<li" in html
    assert "<strong>买入</strong>" in html
    assert "<a href='https://example.com'>公告</a>" in html
    assert "<code" in html


def test_markdown_to_html_supports_markdown_table():
    markdown = """| 指标 | 数值 |
| --- | --- |
| RSI14 | 56.2 |
| BOLL | 中轨上方 |"""
    html = adviser.markdown_to_html(markdown)
    assert "<table" in html
    assert "<th" in html
    assert "<td" in html


def test_parse_rss_items_reads_basic_item():
    xml_text = """<?xml version=\"1.0\"?>
<rss><channel>
<item><title>新闻标题</title><link>https://example.com/1</link><description>摘要</description><pubDate>2026-03-01</pubDate></item>
</channel></rss>"""
    items = adviser.parse_rss_items(xml_text)
    assert len(items) == 1
    assert items[0]["title"] == "新闻标题"
    assert items[0]["href"] == "https://example.com/1"


def test_nearest_open_trade_date_uses_cache(monkeypatch):
    from datetime import datetime, timezone

    adviser._TRADE_DATE_CACHE["date"] = None
    adviser._TRADE_DATE_CACHE["value"] = None

    class _FakeAk:
        calls = 0

        @staticmethod
        def tool_trade_date_hist_sina():
            _FakeAk.calls += 1
            class _DF:
                empty = False
                columns = ["trade_date"]
                def __getitem__(self, key):
                    class _Series:
                        def tolist(self):
                            return ["2026-03-02", "2026-03-03"]
                    return _Series()
            return _DF()

    import builtins
    orig_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "akshare":
            return _FakeAk
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    now = datetime(2026, 3, 3, 3, 0, tzinfo=timezone.utc)
    first = adviser.nearest_open_trade_date(now)
    second = adviser.nearest_open_trade_date(now)

    assert first.isoformat() == "2026-03-03"
    assert second.isoformat() == "2026-03-03"
    assert _FakeAk.calls == 1


def test_build_result_dict_uses_cached_research_result():
    research = adviser.StockResearchResult(
        stock_code="AAPL",
        stock_name=None,
        contexts=[{"title": "t1"}, {"title": "t2"}],
        advice="建议内容",
        brief_summary="观点=中性；置信度=66/100",
    )

    result = adviser.build_result_dict(research)

    assert result == {
        "stock_code": "AAPL",
        "stock_name": "AAPL",
        "search_context_count": 2,
        "brief_summary": "观点=中性；置信度=66/100",
        "advice": "建议内容",
    }


def test_build_brief_summary_extracts_key_fields():
    advice = """
# AAPL 投研结论
研究置信度：78
趋势强度：63
建议：看多，短线可持有/加仓
"""

    summary = adviser.build_brief_summary("AAPL", advice)

    assert "观点=看多" in summary
    assert "置信度=78/100" in summary
    assert "趋势强度=63/100" in summary


def test_build_email_message_lists_summary_before_details():
    config = adviser.Config(
        llm_provider="aihubmix",
        aihubmix_api_key="k",
        aihubmix_base_url="https://api.aihubmix.com/v1",
        aihubmix_model="gpt-4o-mini",
        stock_codes=["AAPL"],
        max_search_results=5,
        search_region="zh-cn",
        sender_email="noreply@test.com",
    )
    research = adviser.StockResearchResult(
        stock_code="AAPL",
        stock_name="苹果",
        contexts=[{"title": "t1"}],
        advice="# AAPL 投研结论\n详细分析正文",
        brief_summary="观点=中性；置信度=70/100",
    )

    message = adviser.build_email_message(config, "a@test.com", ["AAPL"], {"AAPL": research})

    html_body = adviser.extract_html_body(message)
    assert "快速摘要" in html_body
    assert "AAPL（苹果）" in html_body
    assert html_body.index("观点=中性；置信度=70/100") < html_body.index("详细分析正文")


def test_build_queries_contains_global_macro_topics():
    queries = adviser.build_queries("AAPL")
    assert any("海外新闻" in q for q in queries)
    assert any("全球市场" in q for q in queries)




def test_temporary_search_proxy_env_is_noop_without_proxy_side_effect(monkeypatch):
    monkeypatch.delenv("HTTP_PROXY", raising=False)

    with adviser.temporary_search_proxy_env("AAPL"):
        assert adviser.os.environ.get("HTTP_PROXY") is None


def test_request_with_retry_retries_on_timeout_then_succeeds():
    import requests

    calls = {"count": 0}

    def flaky_call():
        calls["count"] += 1
        if calls["count"] < 3:
            raise requests.Timeout("timeout")
        return "ok"

    assert adviser.request_with_retry(flaky_call, retries=3, backoff_seconds=0) == "ok"
    assert calls["count"] == 3


def test_now_shanghai_returns_shanghai_timezone():
    now = adviser.now_shanghai()
    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 8 * 3600


def test_load_config_supports_nim_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "nim")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nim-key")
    monkeypatch.setenv("NVIDIA_NIM_MODEL", "meta/llama-3.1-8b-instruct")
    monkeypatch.setenv("STOCK_CODES", "AAPL")

    config = adviser.load_config()

    assert config.llm_provider == "nim"
    assert config.nim_api_key == "nim-key"
    assert config.nim_model == "meta/llama-3.1-8b-instruct"


def test_load_config_uses_nim_default_model_when_env_missing(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "nim")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nim-key")
    monkeypatch.delenv("NVIDIA_NIM_MODEL", raising=False)
    monkeypatch.delenv("NIM_MODEL", raising=False)
    monkeypatch.setenv("STOCK_CODES", "AAPL")

    config = adviser.load_config()

    assert config.nim_model == "deepseek-ai/deepseek-r1"


def test_load_config_uses_nim_default_base_url_when_env_empty(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "nim")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nim-key")
    monkeypatch.setenv("NVIDIA_NIM_BASE_URL", "")
    monkeypatch.setenv("STOCK_CODES", "AAPL")

    config = adviser.load_config()

    assert config.nim_base_url == "https://integrate.api.nvidia.com/v1"


def test_load_config_raises_when_nim_key_missing(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "nim")
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    monkeypatch.delenv("NIM_API_KEY", raising=False)
    monkeypatch.setenv("STOCK_CODES", "AAPL")

    try:
        adviser.load_config()
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "NVIDIA_NIM_API_KEY" in str(exc)


def test_detect_stock_name_fallbacks_to_yahoo_for_non_cn_symbol(monkeypatch):
    monkeypatch.setattr(adviser, "fetch_cn_stock_name", lambda code: None)
    monkeypatch.setattr(adviser, "fetch_yahoo_stock_name", lambda code: "Apple Inc.")

    detected = adviser.detect_stock_name("AAPL", None)

    assert detected == "Apple Inc."




def test_detect_stock_name_from_contexts_extracts_name_from_title():
    contexts = [
        {"title": "Apple Inc. (AAPL) latest earnings", "body": ""},
    ]

    detected = adviser.detect_stock_name_from_contexts("AAPL", contexts)

    assert detected == "Apple Inc."


def test_detect_stock_name_from_contexts_extracts_name_from_body_with_colon():
    contexts = [
        {"title": "market recap", "body": "AAPL: Apple Inc."},
    ]

    detected = adviser.detect_stock_name_from_contexts("AAPL", contexts)

    assert detected == "Apple Inc."

def test_load_config_reads_search_reflection_max_rounds(monkeypatch):
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("STOCK_CODES", "AAPL")
    monkeypatch.setenv("SEARCH_REFLECTION_MAX_ROUNDS", "4")

    config = adviser.load_config()

    assert config.search_reflection_max_rounds == 4


def test_load_config_reads_information_assessment_rounds(monkeypatch):
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("STOCK_CODES", "AAPL")
    monkeypatch.setenv("INFORMATION_ASSESSMENT_ROUNDS", "5")

    config = adviser.load_config()

    assert config.search_reflection_max_rounds == 5


def test_load_config_information_assessment_rounds_overrides_legacy_env(monkeypatch):
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("STOCK_CODES", "AAPL")
    monkeypatch.setenv("INFORMATION_ASSESSMENT_ROUNDS", "6")
    monkeypatch.setenv("SEARCH_REFLECTION_MAX_ROUNDS", "2")

    config = adviser.load_config()

    assert config.search_reflection_max_rounds == 6


def test_refine_context_with_ai_continues_when_followup_search_has_no_results(monkeypatch):
    config = adviser.Config(
        llm_provider="aihubmix",
        aihubmix_api_key="k",
        aihubmix_base_url="https://api.aihubmix.com/v1",
        aihubmix_model="gpt-4o-mini",
        stock_codes=["AAPL"],
        max_search_results=5,
        search_region="zh-cn",
        search_reflection_max_rounds=2,
    )
    assess_calls = {"count": 0}

    def fake_assess(*args, **kwargs):
        assess_calls["count"] += 1
        return False, ["AAPL guidance"], "still insufficient"

    monkeypatch.setattr(adviser, "assess_information_sufficiency", fake_assess)
    monkeypatch.setattr(adviser, "search_context_via_queries", lambda *args, **kwargs: [])

    refined = adviser.refine_context_with_ai(config, "AAPL", [{"title": "seed"}], "gpt-4o-mini")

    assert assess_calls["count"] == 2
    assert refined == [{"title": "seed"}]


def test_parse_json_object_from_text_supports_fenced_json():
    raw = """```json
{"sufficient": false, "reason": "数据不足", "followup_queries": ["AAPL 指引"]}
```"""

    data = adviser.parse_json_object_from_text(raw)

    assert data["sufficient"] is False
    assert data["reason"] == "数据不足"


def test_wait_for_llm_rate_limit_sleeps_when_called_too_fast(monkeypatch):
    sleep_calls = []
    clock = {"value": 100.0}

    def fake_monotonic():
        return clock["value"]

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        clock["value"] += seconds

    monkeypatch.setattr(adviser.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(adviser.time, "sleep", fake_sleep)
    monkeypatch.setattr(adviser, "realtime_print", lambda *args, **kwargs: None)
    adviser._LLM_RATE_LIMIT_STATE["last_request_at"] = 95.0

    adviser.wait_for_llm_rate_limit("AAPL", "策略生成")

    assert len(sleep_calls) == 1
    assert round(sleep_calls[0], 1) == 7.5
    assert adviser._LLM_RATE_LIMIT_STATE["last_request_at"] == clock["value"]


def test_wait_for_llm_rate_limit_does_not_sleep_when_interval_is_enough(monkeypatch):
    sleep_calls = []

    monkeypatch.setattr(adviser.time, "monotonic", lambda: 200.0)
    monkeypatch.setattr(adviser.time, "sleep", lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr(adviser, "realtime_print", lambda *args, **kwargs: None)
    adviser._LLM_RATE_LIMIT_STATE["last_request_at"] = 180.0

    adviser.wait_for_llm_rate_limit("AAPL", "信息充分性评估")

    assert sleep_calls == []
    assert adviser._LLM_RATE_LIMIT_STATE["last_request_at"] == 200.0
