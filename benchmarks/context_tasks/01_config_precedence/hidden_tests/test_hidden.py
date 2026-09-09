from config import resolve_config


def test_tenant_false_and_zero_beat_environment():
    result = resolve_config(
        {}, {"debug": False, "page_size": 0}, {"APP_DEBUG": "yes", "APP_PAGE_SIZE": "20"}
    )
    assert result["debug"] is False
    assert result["page_size"] == 0


def test_request_empty_string_is_an_explicit_tenant_contract_value():
    result = resolve_config({"theme": ""}, {"theme": "dark"}, {})
    assert result["theme"] == ""


def test_environment_is_coerced_when_higher_layers_are_missing():
    result = resolve_config({}, {}, {"APP_DEBUG": " no ", "APP_PAGE_SIZE": " 25 "})
    assert result["debug"] is False
    assert result["page_size"] == 25
