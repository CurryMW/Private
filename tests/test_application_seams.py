import json
from datetime import UTC, datetime

import httpx
import pytest

from ai_daily.app import AIDigestApplication, AIDigestRuntime, RunStatus
from ai_daily.application import DeliveryStateFileStore, SentStateFileStore
from ai_daily.config import Settings, SourceConfig
from ai_daily.delivery_state import DeliveryState
from ai_daily.dingtalk import DingTalkError
from ai_daily.github_trends_app import (
    GitHubTrendsApplication,
    GitHubTrendsContext,
    GitHubTrendsPreparation,
    GitHubTrendsReport,
    GitHubTrendsRunResult,
    GitHubTrendsRuntime,
    GitHubTrendsState,
)
from ai_daily.state import SentState


NOW = datetime(2026, 7, 18, 0, 30, tzinfo=UTC)
RELEASE_URL = "https://github.com/example/project/releases/tag/v2.0.0"


class MemoryStore:
    def __init__(self, value) -> None:
        self.value = value
        self.saved = []

    def load(self):
        return self.value

    def save(self, value) -> None:
        self.value = value
        self.saved.append(value)


class SenderSpy:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.messages = []

    async def send(self, parts, title) -> None:
        if self.error is not None:
            raise self.error
        self.messages.append((list(parts), title))


def client_factory(handler):
    def create_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return create_client


def digest_settings(tmp_path) -> Settings:
    return Settings(
        ai_api_key="test-ai-key",
        ai_base_url="https://model.example/v1",
        dingtalk_webhook=(
            "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        ),
        state_path=tmp_path / "unused-sent.json",
        delivery_state_path=tmp_path / "unused-deliveries.json",
        enforce_daily_once=True,
    )


def model_response() -> httpx.Response:
    content = {
        "overview": "今天的更新聚焦新的推理运行时及其工程价值。",
        "items": [
            {
                "title": "Inference runtime v2.0",
                "category": "开源工具",
                "source": "GitHub: example/project",
                "summary": "项目发布了新的推理运行时，并提供了技术说明。",
                "impact": "开发者可以据此评估新的部署能力与工程适用性。",
                "url": RELEASE_URL,
            }
        ],
        "trends": ["推理部署工具持续演进", "开源工程能力受到关注"],
    }
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(content)}}]},
    )


@pytest.mark.asyncio
async def test_ai_digest_entry_runs_with_injected_http_state_clock_and_sender(
    tmp_path,
) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "Inference runtime v2.0",
                        "tag_name": "v2.0.0",
                        "body": "New model inference runtime.",
                        "html_url": RELEASE_URL,
                        "published_at": "2026-07-17T23:30:00Z",
                        "draft": False,
                        "prerelease": False,
                    }
                ],
            )
        if request.url.host == "model.example":
            return model_response()
        raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")

    sent_path = tmp_path / "sent.json"
    delivery_path = tmp_path / "deliveries.json"
    sent_store = SentStateFileStore(sent_path)
    delivery_store = DeliveryStateFileStore(delivery_path)
    sender = SenderSpy()
    runtime = AIDigestRuntime(
        clock=lambda: NOW,
        http_client_factory=client_factory(handler),
        sent_state_store=sent_store,
        delivery_state_store=delivery_store,
        sender_factory=lambda client, settings: sender,
    )
    application = AIDigestApplication(
        digest_settings(tmp_path),
        SourceConfig(github_repositories=["example/project"]),
        runtime=runtime,
    )

    result = await application.run()

    assert result.status is RunStatus.SENT
    assert result.failure_type is None
    assert [request.url.host for request in requests] == [
        "api.github.com",
        "model.example",
    ]
    assert sender.messages[0][1] == "AI 技术日报｜2026-07-18"
    assert SentState.load(sent_path).is_sent(RELEASE_URL)
    assert DeliveryState.load(delivery_path).is_delivered(NOW.date())


@pytest.mark.asyncio
async def test_ai_digest_entry_returns_an_explicit_failed_result(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "Inference runtime v2.0",
                        "tag_name": "v2.0.0",
                        "body": "New model inference runtime.",
                        "html_url": RELEASE_URL,
                        "published_at": "2026-07-17T23:30:00Z",
                        "draft": False,
                        "prerelease": False,
                    }
                ],
            )
        return model_response()

    runtime = AIDigestRuntime(
        clock=lambda: NOW,
        http_client_factory=client_factory(handler),
        sent_state_store=MemoryStore(SentState()),
        delivery_state_store=MemoryStore(DeliveryState()),
        sender_factory=lambda client, settings: SenderSpy(
            DingTalkError("safe delivery failure")
        ),
    )
    application = AIDigestApplication(
        digest_settings(tmp_path),
        SourceConfig(github_repositories=["example/project"]),
        runtime=runtime,
    )

    result = await application.run()

    assert result.status is RunStatus.FAILED
    assert result.failure_type == "DingTalkError"


@pytest.mark.asyncio
async def test_github_trends_entry_uses_its_own_runtime_and_state() -> None:
    initial_state = GitHubTrendsState()
    updated_state = GitHubTrendsState(baseline_at=NOW)
    github_state = MemoryStore(initial_state)
    sender = SenderSpy()
    observed = {}

    async def prepare_report(
        context: GitHubTrendsContext,
    ) -> GitHubTrendsPreparation:
        observed["context"] = context
        response = await context.http_client.get(
            "https://api.github.com/search/repositories"
        )
        response.raise_for_status()
        return GitHubTrendsPreparation(
            next_state=updated_state,
            report=GitHubTrendsReport(
                title="GitHub AI 趋势",
                parts=("trend report",),
                repository_count=len(response.json()["items"]),
            ),
            repository_count=5,
        )

    runtime = GitHubTrendsRuntime(
        clock=lambda: NOW,
        http_client_factory=client_factory(
            lambda request: httpx.Response(
                200, json={"items": [{"id": index} for index in range(5)]}
            )
        ),
        state_store=github_state,
        sender_factory=lambda client: sender,
    )

    result = await GitHubTrendsApplication(prepare_report, runtime).run()

    assert result == GitHubTrendsRunResult(
        status=RunStatus.SENT,
        repository_count=5,
        part_count=1,
    )
    assert observed["context"].run_at == NOW
    assert observed["context"].state is initial_state
    assert github_state.value is updated_state
    assert github_state.saved == [updated_state]
    assert sender.messages == [(["trend report"], "GitHub AI 趋势")]


@pytest.mark.asyncio
async def test_github_trends_entry_produces_empty_and_saves_its_baseline() -> None:
    initial_state = GitHubTrendsState()
    baseline = GitHubTrendsState(baseline_at=NOW)
    github_state = MemoryStore(initial_state)

    async def prepare_empty(
        context: GitHubTrendsContext,
    ) -> GitHubTrendsPreparation:
        return GitHubTrendsPreparation(
            next_state=baseline,
            report=None,
            repository_count=0,
        )

    runtime = GitHubTrendsRuntime(
        clock=lambda: NOW,
        http_client_factory=client_factory(
            lambda request: httpx.Response(500)
        ),
        state_store=github_state,
        sender_factory=lambda client: SenderSpy(),
    )

    result = await GitHubTrendsApplication(prepare_empty, runtime).run()

    assert result == GitHubTrendsRunResult(
        status=RunStatus.EMPTY,
        repository_count=0,
        part_count=0,
    )
    assert github_state.saved == [baseline]


@pytest.mark.asyncio
async def test_github_trends_entry_reports_failure_without_log_parsing() -> None:
    async def fail(context: GitHubTrendsContext) -> GitHubTrendsPreparation:
        raise RuntimeError("upstream detail")

    runtime = GitHubTrendsRuntime(
        clock=lambda: NOW,
        http_client_factory=client_factory(
            lambda request: httpx.Response(500)
        ),
        state_store=MemoryStore(GitHubTrendsState()),
        sender_factory=lambda client: SenderSpy(),
    )

    result = await GitHubTrendsApplication(fail, runtime).run()

    assert result.status is RunStatus.FAILED
    assert result.failure_type == "RuntimeError"
    assert result.repository_count == 0
    assert result.part_count == 0
