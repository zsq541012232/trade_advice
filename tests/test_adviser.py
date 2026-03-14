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
    assert len(queries) == 3
    assert "新闻" in queries[0]
    assert "财报" in queries[1]
    assert "技术指标" in queries[2]


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
    assert "短线建议" in prompt
    assert "长线建议" in prompt
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
        search_region="cn-zh",
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
        search_region="cn-zh",
    )
    req = _FakeRequests(_FakeResponse({"data": [{"id": "deepseek-v3"}, {"id": "qwen-plus"}]}))

    model = adviser.resolve_model(config, req)

    assert model == "deepseek-v3"


def test_stock_code_aliases_for_shanghai_code():
    aliases = adviser.stock_code_aliases("600900")
    assert aliases == ["600900", "600900.SH", "SH600900", "上证600900"]


def test_build_queries_expands_aliases_for_a_share_code():
    queries = adviser.build_queries("600900")
    assert len(queries) == 12
    assert "600900 新闻 舆情 最新" in queries
    assert "600900.SH 财报 业绩 指引" in queries
    assert "SH600900 股价 技术指标 成交量 RSI MACD" in queries
