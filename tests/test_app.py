import io
import logging

import pytest

from ai_daily import app, cli
from ai_daily.analyzer import AnalysisError
from ai_daily.baidu_search import BaiduSearchError
from ai_daily.config import Settings, SourceConfig, BaiduSearchConfig
from ai_daily.dingtalk import DingTalkError


def settings(tmp_path):
    return Settings(
        ai_api_key="test-ai-key",
        ai_model="gpt-5.6-luna",
        dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=test-token",
        state_path=tmp_path / "sent.json",
        delivery_state_path=tmp_path / "deliveries.json",
    )


def source_configuration():
    return SourceConfig(baidu_search=BaiduSearchConfig(
        fixed_queries=[f"AI update {i}" for i in range(16)],
        rotating_queries=[f"AI research {i}" for i in range(4)],
    ))


@pytest.mark.parametrize("status", ["sent", "dry-run", "empty"])
def test_cli_returns_zero_and_prints_only_final_counts(
    status, tmp_path, monkeypatch, capsys
) -> None:
    result = app.RunResult(
        status=status, candidate_count=4, selected_count=2, part_count=1
    )
    configured_settings = settings(tmp_path)
    source_config = source_configuration()
    calls = []

    monkeypatch.setattr(cli, "load_dotenv", lambda: calls.append("dotenv"))
    monkeypatch.setattr(cli, "load_settings", lambda: configured_settings)
    monkeypatch.setattr(
        cli,
        "load_source_config",
        lambda path: calls.append(path) or source_config,
    )

    async def run(received_settings, received_source_config):
        assert received_settings is configured_settings
        assert received_source_config is source_config
        return result

    monkeypatch.setattr(cli, "run_digest", run)

    assert cli.main() == 0
    assert calls == ["dotenv", cli.Path("config/sources.yaml")]
    assert capsys.readouterr().out == (
        f"status={status} candidates=4 selected=2 parts=1\n"
    )


@pytest.mark.parametrize(
    ("error", "safe_message"),
    [
        (ValueError("secret-value"), "configuration is invalid"),
        (AnalysisError("analysis validation failed"), "analysis validation failed"),
        (
            BaiduSearchError("BAIDU_SEARCH_API_KEY is required"),
            "BAIDU_SEARCH_API_KEY is required",
        ),
        (DingTalkError("DingTalk delivery failed"), "DingTalk delivery failed"),
    ],
)
def test_cli_returns_one_with_safe_error_logging(
    error, safe_message, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "load_settings", lambda: (_ for _ in ()).throw(error))

    assert cli.main() == 1
    assert type(error).__name__ in caplog.text
    assert safe_message in caplog.text
    assert "secret-value" not in caplog.text


def test_cli_returns_one_when_run_digest_raises(tmp_path, monkeypatch, caplog) -> None:
    configured_settings = settings(tmp_path)
    source_config = source_configuration()
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "load_settings", lambda: configured_settings)
    monkeypatch.setattr(cli, "load_source_config", lambda path: source_config)

    async def fail(*args, **kwargs):
        raise DingTalkError("safe pipeline failure")

    monkeypatch.setattr(cli, "run_digest", fail)

    assert cli.main() == 1
    assert "DingTalkError" in caplog.text
    assert "safe pipeline failure" in caplog.text


def test_cli_logging_suppresses_dependency_request_urls(
    tmp_path, monkeypatch
) -> None:
    configured_settings = settings(tmp_path)
    source_config = source_configuration()
    result = app.RunResult(
        status="empty", candidate_count=0, selected_count=0, part_count=0
    )
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "load_settings", lambda: configured_settings)
    monkeypatch.setattr(cli, "load_source_config", lambda path: source_config)

    async def run(*args, **kwargs):
        return result

    monkeypatch.setattr(cli, "run_digest", run)

    root_logger = logging.getLogger()
    ai_daily_logger = logging.getLogger("ai_daily")
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    original_handlers = list(root_logger.handlers)
    original_levels = {
        root_logger: root_logger.level,
        ai_daily_logger: ai_daily_logger.level,
        httpx_logger: httpx_logger.level,
        httpcore_logger: httpcore_logger.level,
    }
    for handler in original_handlers:
        root_logger.removeHandler(handler)

    try:
        assert cli.main() == 0
        assert ai_daily_logger.getEffectiveLevel() == logging.INFO
        assert httpx_logger.getEffectiveLevel() >= logging.WARNING
        assert httpcore_logger.getEffectiveLevel() >= logging.WARNING

        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        root_logger.addHandler(handler)
        token = "never-log-this-access-token"
        httpx_logger.info(
            "HTTP Request: POST https://oapi.dingtalk.com/robot/send?access_token=%s",
            token,
        )
        handler.flush()
        assert token not in captured.getvalue()
    finally:
        root_logger.handlers.clear()
        root_logger.handlers.extend(original_handlers)
        for configured_logger, level in original_levels.items():
            configured_logger.setLevel(level)
