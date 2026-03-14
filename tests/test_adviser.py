import pytest
import adviser


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

    with pytest.raises(ValueError, match="DUCKDUCKGO_MAX_RESULTS 必须是整数"):
        adviser.load_config()


def test_load_config_raises_when_max_results_is_not_positive(monkeypatch):
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("STOCK_CODES", "AAPL")
    monkeypatch.setenv("DUCKDUCKGO_MAX_RESULTS", "0")

    with pytest.raises(ValueError, match="DUCKDUCKGO_MAX_RESULTS 必须大于 0"):
        adviser.load_config()


def test_load_config_uses_default_base_url_when_empty(monkeypatch):
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("STOCK_CODES", "AAPL")
    monkeypatch.setenv("AIHUBMIX_BASE_URL", "")

    config = adviser.load_config()

    assert config.aihubmix_base_url == "https://api.aihubmix.com/v1"


def test_load_config_raises_when_base_url_is_missing_scheme(monkeypatch):
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("STOCK_CODES", "AAPL")
    monkeypatch.setenv("AIHUBMIX_BASE_URL", "api.aihubmix.com/v1")

    with pytest.raises(ValueError, match=r"AIHUBMIX_BASE_URL 必须是有效的 http\(s\) URL"):
        adviser.load_config()
