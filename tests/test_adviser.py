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
    assert len(queries) == 4
    assert "最新消息" in queries[0]
    assert "财报" in queries[1]
    assert "技术指标" in queries[2]
    assert "宏观政策" in queries[3]


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
    assert "短线策略（1天~2周）" in prompt
    assert "长线策略（3个月~3年）" in prompt
    assert "情景分析" in prompt
    assert "研究置信度" in prompt
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


def test_stock_code_aliases_for_shanghai_code():
    aliases = adviser.stock_code_aliases("600900")
    assert aliases == ["600900", "600900.SH", "上证600900"]


def test_build_queries_expands_aliases_for_a_share_code():
    queries = adviser.build_queries("600900")
    assert len(queries) == 8
    assert "600900 股票 最新消息" in queries
    assert "600900.SH 财报 业绩 指引" in queries
    assert "600900 银行业 宏观政策 影响" in queries


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


def test_calculate_indicators_returns_none_for_short_series():
    indicators = adviser.calculate_indicators([1.0, 1.1, 1.2])
    assert indicators["rsi14"] is None
    assert indicators["macd"] is None
    assert indicators["kdj_k"] is None


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
        contexts=[{"title": "t1"}, {"title": "t2"}],
        advice="建议内容",
    )

    result = adviser.build_result_dict(research)

    assert result == {
        "stock_code": "AAPL",
        "search_context_count": 2,
        "advice": "建议内容",
    }
