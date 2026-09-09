from config import resolve_config


def test_request_can_explicitly_disable_debug_and_zero_retention():
    result = resolve_config(
        {"debug": False, "audit_retention_days": 0},
        {"debug": True, "audit_retention_days": 90},
        {"APP_DEBUG": "true", "APP_AUDIT_RETENTION_DAYS": "365"},
    )
    assert result["debug"] is False
    assert result["audit_retention_days"] == 0


def test_blank_environment_value_is_absent():
    result = resolve_config({}, {}, {"APP_THEME": "   "})
    assert result["theme"] == "light"
