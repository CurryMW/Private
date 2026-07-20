import io
import logging
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_daily import app, cli
from ai_daily.analyzer import AnalysisError
from ai_daily.config import Settings, SourceConfig
from ai_daily.delivery_state import DeliveryState
from ai_daily.dingtalk import DingTalkError
from ai_daily.filtering import candidate_id
from ai_daily.models import Candidate, Category, Digest, DigestItem
from ai_daily.state import SentState


NOW = datetime(2026, 7, 18, 8, 30, tzinfo=timezone(timedelta(hours=8)))
NOW_UTC = datetime(2026, 7, 18, 0, 30, tzinfo=UTC)


def settings(
    tmp_path,
    *,
    dry_run: bool = False,
    enforce_daily_once: bool = False,
) -> Settings:
    return Settings(
        ai_api_key="test-ai-key",
        dingtalk_webhook=(
            "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        ),
        window_hours=36,
        fallback_window_hours=168,
        timezone="Asia/Shanghai",
        dry_run=dry_run,
        state_path=tmp_path / "sent.json",
        delivery_state_path=tmp_path / "deliveries.json",
        enforce_daily_once=enforce_daily_once,
        github_token="test-github-token",
    )


def candidate(
    url: str,
    *,
    hours_old: int = 1,
    title: str = "A technical model update",
) -> Candidate:
    published_at = NOW_UTC - timedelta(hours=hours_old)
    return Candidate(
        id=candidate_id(url),
        title=title,
        summary="Technical details about model inference and training.",
        source="Example Research",
        url=url,
        published_at=published_at,
        source_kind="rss",
    )


def digest_for(selected: Candidate) -> Digest:
    return Digest(
        overview="A sufficiently detailed overview of today's AI updates.",
        items=[
            DigestItem(
                title=selected.title,
                category=Category.MODEL,
                source=selected.source,
                summary="A sufficiently detailed factual summary.",
                impact="A sufficiently detailed explanation of the impact.",
                url=selected.url,
            )
        ],
        trends=[
            "A sufficiently detailed first technical trend.",
            "A sufficiently detailed second technical trend.",
        ],
    )


def install_client_spy(monkeypatch):
    instances = []

    class ClientSpy:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.exited = False
            self.exit_args = None
            instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            self.exited = True
            self.exit_args = (exc_type, exc, traceback)
            return None

    monkeypatch.setattr(app.httpx, "AsyncClient", ClientSpy)
    return instances


@pytest.mark.asyncio
async def test_completed_report_date_skips_before_network_work(
    tmp_path, monkeypatch
) -> None:
    run_settings = settings(tmp_path, enforce_daily_once=True)
    delivery_state = DeliveryState()
    delivery_state.mark_delivered(NOW.date(), NOW_UTC)
    delivery_state.save(run_settings.delivery_state_path)

    def fail_client(**kwargs):
        raise AssertionError("HTTP client must not be created")

    monkeypatch.setattr(app.httpx, "AsyncClient", fail_client)

    result = await app.run_digest(
        run_settings,
        SourceConfig(github_repositories=["owner/repository"]),
        now=NOW,
    )

    assert result == app.RunResult(
        status="already-sent",
        candidate_count=0,
        selected_count=0,
        part_count=0,
    )


@pytest.mark.asyncio
async def test_normal_run_uses_one_client_and_saves_only_selected_urls(
    tmp_path, monkeypatch
) -> None:
    run_settings = settings(tmp_path, enforce_daily_once=True)
    source_config = SourceConfig(github_repositories=["owner/repository"])
    selected = candidate("https://example.com/selected")
    unselected = candidate(
        "https://example.com/unselected",
        hours_old=2,
        title="A new open source inference toolkit",
    )
    observations = {}
    clients = install_client_spy(monkeypatch)

    async def collect(config, client, cutoff, now, github_token):
        observations["collect"] = (config, client, cutoff, now, github_token)
        return [unselected, selected]

    class AnalyzerSpy:
        def __init__(self, client, configured_settings) -> None:
            observations["analyzer_client"] = client
            assert configured_settings is run_settings

        async def analyze(self, candidates, max_items=None):
            observations["prepared"] = candidates
            observations["max_items"] = max_items
            return digest_for(selected)

    def render(digest, report_date, window_hours, **kwargs):
        observations["render"] = (digest, report_date, window_hours, kwargs)
        return ["part one", "part two"]

    class SenderSpy:
        def __init__(self, client, configured_settings) -> None:
            observations["sender_client"] = client
            assert configured_settings is run_settings

        async def send(self, parts, title):
            observations["send"] = (list(parts), title)

    monkeypatch.setattr(app, "collect_candidates", collect)
    monkeypatch.setattr(app, "Analyzer", AnalyzerSpy)
    monkeypatch.setattr(app, "render_digest", render)
    monkeypatch.setattr(app, "DingTalkSender", SenderSpy)

    result = await app.run_digest(run_settings, source_config, now=NOW)

    assert result == app.RunResult(
        status="sent", candidate_count=2, selected_count=1, part_count=2
    )
    assert len(clients) == 1
    assert clients[0].kwargs == {
        "headers": {"User-Agent": "dingtalk-ai-daily/0.1"}
    }
    assert observations["collect"] == (
        source_config,
        clients[0],
        NOW_UTC - timedelta(hours=168),
        NOW_UTC,
        "test-github-token",
    )
    assert observations["analyzer_client"] is clients[0]
    assert observations["sender_client"] is clients[0]
    assert observations["prepared"] == [selected, unselected]
    assert observations["max_items"] == 8
    assert observations["render"][1].isoformat() == "2026-07-18"
    assert observations["render"][2] == 36
    assert observations["render"][3] == {
        "report_title": "AI 技术日报",
        "intro": None,
        "scope_text": None,
    }
    assert observations["send"] == (
        ["part one", "part two"],
        "AI 技术日报｜2026-07-18",
    )
    saved_state = SentState.load(run_settings.state_path)
    assert saved_state.is_sent(str(selected.url))
    assert not saved_state.is_sent(str(unselected.url))
    assert DeliveryState.load(
        run_settings.delivery_state_path
    ).is_delivered(NOW.date())


@pytest.mark.asyncio
async def test_dry_run_previews_parts_without_sender_or_state_mutation(
    tmp_path, monkeypatch, capsys
) -> None:
    run_settings = settings(
        tmp_path, dry_run=True, enforce_daily_once=True
    )
    selected = candidate("https://example.com/dry-run")
    existing = SentState()
    existing.mark_sent(["https://example.com/existing"], NOW_UTC)
    existing.save(run_settings.state_path)
    original_state = run_settings.state_path.read_bytes()
    install_client_spy(monkeypatch)

    async def collect(*args, **kwargs):
        return [selected]

    class AnalyzerStub:
        def __init__(self, client, configured_settings) -> None:
            pass

        async def analyze(self, candidates, max_items=None):
            return digest_for(selected)

    def sender_must_not_be_constructed(*args, **kwargs):
        raise AssertionError("dry run constructed DingTalkSender")

    monkeypatch.setattr(app, "collect_candidates", collect)
    monkeypatch.setattr(app, "Analyzer", AnalyzerStub)
    monkeypatch.setattr(
        app, "render_digest", lambda *args, **kwargs: ["first", "second"]
    )
    monkeypatch.setattr(app, "DingTalkSender", sender_must_not_be_constructed)

    result = await app.run_digest(
        run_settings,
        SourceConfig(github_repositories=["owner/repository"]),
        now=NOW,
    )

    assert result == app.RunResult(
        status="dry-run", candidate_count=1, selected_count=1, part_count=2
    )
    assert capsys.readouterr().out == (
        "--- preview 1/2 ---\nfirst\n--- preview 2/2 ---\nsecond\n"
    )
    assert run_settings.state_path.read_bytes() == original_state
    assert not SentState.load(run_settings.state_path).is_sent(str(selected.url))
    assert not run_settings.delivery_state_path.exists()


@pytest.mark.asyncio
async def test_notice_mode_sends_and_marks_delivery_without_url_state(
    tmp_path, monkeypatch, caplog
) -> None:
    run_settings = settings(tmp_path, enforce_daily_once=True)
    stale = candidate("https://example.com/stale", hours_old=169)
    install_client_spy(monkeypatch)
    observations = {}

    async def collect(*args, **kwargs):
        return [stale]

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("notice mode called the analyzer or digest renderer")

    class SenderSpy:
        def __init__(self, client, configured_settings) -> None:
            pass

        async def send(self, parts, title):
            observations["send"] = (list(parts), title)

    monkeypatch.setattr(app, "collect_candidates", collect)
    monkeypatch.setattr(app, "Analyzer", must_not_be_called)
    monkeypatch.setattr(app, "render_digest", must_not_be_called)
    monkeypatch.setattr(app, "render_status_notice", lambda report_date: ["notice"])
    monkeypatch.setattr(app, "DingTalkSender", SenderSpy)

    with caplog.at_level(logging.INFO, logger="ai_daily.app"):
        result = await app.run_digest(
            run_settings,
            SourceConfig(github_repositories=["owner/repository"]),
            now=NOW,
        )

    assert result == app.RunResult(
        status="sent", candidate_count=0, selected_count=0, part_count=1
    )
    assert not run_settings.state_path.exists()
    assert observations["send"] == (
        ["notice"],
        "AI 技术日报｜2026-07-18",
    )
    assert DeliveryState.load(
        run_settings.delivery_state_path
    ).is_delivered(NOW.date())
    assert "selected=0" in caplog.messages
    assert "parts=1" in caplog.messages
    assert "mode=notice" in caplog.messages


@pytest.mark.asyncio
async def test_extended_mode_uses_unseen_seven_day_content(
    tmp_path, monkeypatch
) -> None:
    run_settings = settings(tmp_path, dry_run=True)
    selected = candidate("https://example.com/extended", hours_old=72)
    observations = {}
    install_client_spy(monkeypatch)

    async def collect(*args, **kwargs):
        return [selected]

    class AnalyzerSpy:
        def __init__(self, client, configured_settings) -> None:
            pass

        async def analyze(self, candidates, max_items=None):
            observations["analysis"] = (candidates, max_items)
            return digest_for(selected)

    def render(digest, report_date, window_hours, **kwargs):
        observations["render"] = (window_hours, kwargs)
        return ["preview"]

    monkeypatch.setattr(app, "collect_candidates", collect)
    monkeypatch.setattr(app, "Analyzer", AnalyzerSpy)
    monkeypatch.setattr(app, "render_digest", render)

    result = await app.run_digest(
        run_settings,
        SourceConfig(github_repositories=["owner/repository"]),
        now=NOW,
    )

    assert result.status == "dry-run"
    assert observations["analysis"] == ([selected], 8)
    assert observations["render"] == (
        168,
        {
            "report_title": "AI 近期技术精选",
            "intro": None,
            "scope_text": "信息范围：最近 7 天",
        },
    )


@pytest.mark.asyncio
async def test_review_mode_reuses_sent_content_with_three_item_limit(
    tmp_path, monkeypatch
) -> None:
    run_settings = settings(tmp_path, dry_run=True)
    selected = candidate("https://example.com/review", hours_old=72)
    sent_state = SentState()
    sent_state.mark_sent([str(selected.url)], NOW_UTC - timedelta(hours=1))
    sent_state.save(run_settings.state_path)
    observations = {}
    install_client_spy(monkeypatch)

    async def collect(*args, **kwargs):
        return [selected]

    class AnalyzerSpy:
        def __init__(self, client, configured_settings) -> None:
            pass

        async def analyze(self, candidates, max_items=None):
            observations["analysis"] = (candidates, max_items)
            return digest_for(selected)

    def render(digest, report_date, window_hours, **kwargs):
        observations["render"] = (window_hours, kwargs)
        return ["preview"]

    monkeypatch.setattr(app, "collect_candidates", collect)
    monkeypatch.setattr(app, "Analyzer", AnalyzerSpy)
    monkeypatch.setattr(app, "render_digest", render)

    await app.run_digest(
        run_settings,
        SourceConfig(github_repositories=["owner/repository"]),
        now=NOW,
    )

    assert observations["analysis"] == ([selected], 3)
    assert observations["render"] == (
        168,
        {
            "report_title": "AI 近期技术回顾",
            "intro": "今日无新的合格动态，以下为近期值得回顾的技术内容。",
            "scope_text": "回顾范围：最近 7 天",
        },
    )


@pytest.mark.asyncio
async def test_persisted_sent_candidate_is_filtered_before_analysis(
    tmp_path, monkeypatch
) -> None:
    run_settings = settings(tmp_path, dry_run=True)
    already_sent = candidate("https://example.com/already-sent")
    fresh = candidate(
        "https://example.com/fresh",
        title="A fresh technical model update",
    )
    sent_state = SentState()
    sent_state.mark_sent([str(already_sent.url)], NOW_UTC - timedelta(hours=1))
    sent_state.save(run_settings.state_path)
    install_client_spy(monkeypatch)
    analyzed_candidates = []

    async def collect(*args, **kwargs):
        return [already_sent, fresh]

    class AnalyzerSpy:
        def __init__(self, client, configured_settings) -> None:
            pass

        async def analyze(self, candidates, max_items=None):
            analyzed_candidates.extend(candidates)
            return digest_for(fresh)

    monkeypatch.setattr(app, "collect_candidates", collect)
    monkeypatch.setattr(app, "Analyzer", AnalyzerSpy)
    monkeypatch.setattr(
        app, "render_digest", lambda *args, **kwargs: ["preview"]
    )

    result = await app.run_digest(
        run_settings,
        SourceConfig(github_repositories=["owner/repository"]),
        now=NOW,
    )

    assert analyzed_candidates == [fresh]
    assert result.candidate_count == 1


@pytest.mark.asyncio
async def test_report_date_uses_configured_timezone_across_utc_date_boundary(
    tmp_path, monkeypatch
) -> None:
    run_settings = settings(tmp_path, dry_run=True)
    run_at = datetime(2026, 7, 17, 16, 30, tzinfo=UTC)
    selected = candidate("https://example.com/timezone-boundary").model_copy(
        update={"published_at": run_at - timedelta(hours=1)}
    )
    install_client_spy(monkeypatch)
    rendered_dates = []

    async def collect(*args, **kwargs):
        return [selected]

    class AnalyzerStub:
        def __init__(self, client, configured_settings) -> None:
            pass

        async def analyze(self, candidates, max_items=None):
            return digest_for(selected)

    def render(digest, report_date, window_hours, **kwargs):
        rendered_dates.append(report_date)
        return ["preview"]

    monkeypatch.setattr(app, "collect_candidates", collect)
    monkeypatch.setattr(app, "Analyzer", AnalyzerStub)
    monkeypatch.setattr(app, "render_digest", render)

    await app.run_digest(
        run_settings,
        SourceConfig(github_repositories=["owner/repository"]),
        now=run_at,
    )

    assert rendered_dates[0].isoformat() == "2026-07-18"


@pytest.mark.asyncio
async def test_delivery_failure_on_second_part_does_not_save_state(
    tmp_path, monkeypatch
) -> None:
    run_settings = settings(tmp_path, enforce_daily_once=True)
    selected = candidate("https://example.com/send-failure")
    clients = install_client_spy(monkeypatch)

    async def collect(*args, **kwargs):
        return [selected]

    class AnalyzerStub:
        def __init__(self, client, configured_settings) -> None:
            pass

        async def analyze(self, candidates, max_items=None):
            return digest_for(selected)

    class FailingSender:
        def __init__(self, client, configured_settings) -> None:
            pass

        async def send(self, parts, title):
            for index, _ in enumerate(parts, 1):
                if index == 2:
                    raise DingTalkError("safe delivery failure")

    monkeypatch.setattr(app, "collect_candidates", collect)
    monkeypatch.setattr(app, "Analyzer", AnalyzerStub)
    monkeypatch.setattr(
        app, "render_digest", lambda *args, **kwargs: ["first", "second"]
    )
    monkeypatch.setattr(app, "DingTalkSender", FailingSender)

    with pytest.raises(DingTalkError, match="safe delivery failure"):
        await app.run_digest(
            run_settings,
            SourceConfig(github_repositories=["owner/repository"]),
            now=NOW,
        )

    assert not run_settings.state_path.exists()
    assert not run_settings.delivery_state_path.exists()
    assert clients[0].exited is True
    assert clients[0].exit_args[0] is DingTalkError


@pytest.mark.parametrize("status", ["sent", "dry-run", "empty"])
def test_cli_returns_zero_and_prints_only_final_counts(
    status, tmp_path, monkeypatch, capsys
) -> None:
    result = app.RunResult(
        status=status, candidate_count=4, selected_count=2, part_count=1
    )
    configured_settings = settings(tmp_path)
    source_config = SourceConfig(github_repositories=["owner/repository"])
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
    source_config = SourceConfig(github_repositories=["owner/repository"])
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
    source_config = SourceConfig(github_repositories=["owner/repository"])
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
