from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from ai_daily.config import load_settings


BASE_ENV = {
    "AI_API_KEY": "sk-test",
    "DINGTALK_WEBHOOK": "https://oapi.dingtalk.com/robot/send?access_token=test-token",
}


def test_load_settings_uses_approved_defaults() -> None:
    settings = load_settings(BASE_ENV)
    assert settings.ai_base_url == "https://apiclaude.cc/v1"
    assert settings.ai_model == "claude-sonnet-4-6"
    assert settings.window_hours == 36
    assert settings.max_items == 8
    assert settings.timezone == "Asia/Shanghai"
    assert settings.dry_run is False
    assert settings.delivery_state_path == Path(".state/deliveries.json")
    assert settings.enforce_daily_once is False


def test_base_webhook_requires_access_token() -> None:
    env = BASE_ENV | {"DINGTALK_WEBHOOK": "https://oapi.dingtalk.com/robot/send"}
    with pytest.raises(ValueError, match="DINGTALK_ACCESS_TOKEN"):
        load_settings(env)


def test_base_webhook_accepts_separate_access_token() -> None:
    env = BASE_ENV | {
        "DINGTALK_WEBHOOK": "https://oapi.dingtalk.com/robot/send",
        "DINGTALK_ACCESS_TOKEN": "separate-token",
        "DRY_RUN": "true",
        "DELIVERY_STATE_PATH": ".state/custom-deliveries.json",
        "ENFORCE_DAILY_ONCE": "true",
    }
    settings = load_settings(env)
    assert settings.dingtalk_access_token.get_secret_value() == "separate-token"
    assert settings.dry_run is True
    assert settings.delivery_state_path == Path(".state/custom-deliveries.json")
    assert settings.enforce_daily_once is True
    assert parse_qs(urlparse(settings.dingtalk_webhook.get_secret_value()).query) == {}


def test_missing_ai_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="AI_API_KEY"):
        load_settings({"DINGTALK_WEBHOOK": BASE_ENV["DINGTALK_WEBHOOK"]})
