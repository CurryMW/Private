import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ai_daily.application import RunStatus
from ai_daily.dingtalk import DingTalkError
from ai_daily.github_api import GitHubAPIError, GitHubRepositorySearchClient
from ai_daily.github_trends import (
    GitHubTrendsSettings,
    create_github_trends_application,
    load_github_trends_settings,
)
from ai_daily.github_trends_state import GitHubTrendsState
from ai_daily.github_trends_app import GitHubTrendsRunResult


NOW = datetime(2026, 9, 10, 0, 45, tzinfo=UTC)


def repository_payload(
    index: int,
    *,
    stars: int = 100,
    description: str | None = "AI inference runtime for production model serving",
    full_name: str | None = None,
    fork: bool = False,
    archived: bool = False,
    created_at: str = "2026-06-01T00:00:00Z",
    pushed_at: str = "2026-09-09T12:00:00Z",
    topics: list[str] | None = None,
) -> dict[str, object]:
    repository_name = full_name or f"example/inference-{index}"
    return {
        "id": index,
        "name": f"inference-{index}",
        "full_name": repository_name,
        "html_url": f"https://github.com/{repository_name}",
        "description": description,
        "fork": fork,
        "archived": archived,
        "stargazers_count": stars,
        "language": "Python",
        "created_at": created_at,
        "pushed_at": pushed_at,
        "topics": topics if topics is not None else [
            "artificial-intelligence",
            "inference",
        ],
    }


def readme_payload(full_name: str, *, size: int = 1_200) -> dict[str, object]:
    return {
        "type": "file",
        "size": size,
        "html_url": f"https://github.com/{full_name}/blob/main/README.md",
    }


def client_factory(handler, *, readme_handler=None):
    def create_client():
        def route(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/readme"):
                if readme_handler is not None:
                    return readme_handler(request)
                full_name = request.url.path.removeprefix("/repos/").removesuffix(
                    "/readme"
                )
                return httpx.Response(200, json=readme_payload(full_name))
            return handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(route))

    return create_client


def test_github_trends_settings_load_independent_model_delivery_and_state_configuration(
) -> None:
    settings = load_github_trends_settings(
        {
            "GITHUB_TOKEN": "test-github-token",
            "GITHUB_TRENDS_STATE_PATH": ".state/github-trends/custom.json",
            "AI_API_KEY": "test-ai-key",
            "AI_BASE_URL": "https://model.example/v1/",
            "AI_MODEL": "gpt-5.6-luna",
            "DINGTALK_WEBHOOK": (
                "https://oapi.dingtalk.com/robot/send?access_token=test-token"
            ),
            "DRY_RUN": "true",
            "TIMEZONE": "Asia/Shanghai",
        }
    )

    assert settings.github_token.get_secret_value() == "test-github-token"
    assert settings.state_path.as_posix() == ".state/github-trends/custom.json"
    assert settings.ai_api_key.get_secret_value() == "test-ai-key"
    assert settings.ai_base_url == "https://model.example/v1"
    assert settings.ai_model == "gpt-5.6-luna"
    assert settings.dingtalk_webhook.get_secret_value().startswith("https://")
    assert settings.dry_run is True


def test_github_trends_settings_reject_unapproved_model() -> None:
    with pytest.raises(ValueError, match="gpt-5.6-luna"):
        load_github_trends_settings({"AI_MODEL": "fallback-model"})


@pytest.mark.asyncio
async def test_github_trends_entry_builds_first_baseline_without_sending(
    tmp_path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [repository_payload(101, stars=321)],
            },
        )

    state_path = tmp_path / "github-trends.json"

    def sender_must_not_be_constructed(client):
        raise AssertionError("baseline run constructed a sender")

    application = create_github_trends_application(
        GitHubTrendsSettings(
            github_token="test-github-token",
            state_path=state_path,
        ),
        clock=lambda: NOW,
        http_client_factory=client_factory(handler),
        sender_factory=sender_must_not_be_constructed,
    )

    result = await application.run()

    assert result.status is RunStatus.BASELINE_ESTABLISHED
    assert result.repository_count == 1
    assert result.part_count == 0
    assert result.failure_type is None
    assert len(requests) == 1
    state = GitHubTrendsState.load(state_path)
    assert state.baseline_at == NOW
    assert state.latest_at == NOW
    assert state.baseline_repositories["101"].stars == 321
    assert state.baseline_repositories["101"].readme_size == 1_200
    assert state.latest_repositories == state.baseline_repositories
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert "baseline_at" not in payload
    assert "baseline_repositories" not in payload
    sampled_at = datetime.fromisoformat(
        payload["baseline"]["sampled_at"].replace("Z", "+00:00")
    )
    assert sampled_at == NOW
    assert payload["baseline"]["repositories"] == payload["latest"][
        "repositories"
    ]


@pytest.mark.asyncio
async def test_github_trends_entry_pages_stably_and_caps_snapshot_at_two_hundred(
    tmp_path,
) -> None:
    requests: list[httpx.Request] = []
    readme_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["page"])
        first_id = (page - 1) * 100 + 1
        return httpx.Response(
            200,
            json={
                "total_count": 250,
                "incomplete_results": False,
                "items": [
                    repository_payload(repository_id)
                    for repository_id in range(first_id, first_id + 100)
                ],
            },
        )

    state_path = tmp_path / "github-trends.json"
    application = create_github_trends_application(
        GitHubTrendsSettings(
            github_token="test-github-token",
            state_path=state_path,
        ),
        clock=lambda: NOW,
        http_client_factory=client_factory(
            handler,
            readme_handler=lambda request: (
                readme_requests.append(request)
                or httpx.Response(
                    200,
                    json=readme_payload(
                        request.url.path.removeprefix("/repos/").removesuffix(
                            "/readme"
                        )
                    ),
                )
            ),
        ),
        sender_factory=lambda client: (_ for _ in ()).throw(
            AssertionError("snapshot run constructed a sender")
        ),
    )

    result = await application.run()

    assert result.status is RunStatus.BASELINE_ESTABLISHED
    assert result.repository_count == 200
    assert [request.url.params["page"] for request in requests] == ["1", "2"]
    assert len(readme_requests) == 200
    for request in requests:
        assert request.url.params["per_page"] == "100"
        assert request.url.params["sort"] == "updated"
        assert request.url.params["order"] == "desc"
        assert "in:name,description,topics" in request.url.params["q"]
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
        assert request.headers["Authorization"] == "Bearer test-github-token"
    state = GitHubTrendsState.load(state_path)
    assert len(state.baseline_repositories) == 200
    assert set(state.baseline_repositories) == {
        str(repository_id) for repository_id in range(1, 201)
    }


@pytest.mark.asyncio
async def test_github_trends_entry_filters_to_recent_documented_ai_repositories(
    tmp_path,
) -> None:
    items = [
        repository_payload(1),
        repository_payload(2, fork=True),
        repository_payload(3, archived=True),
        repository_payload(4, pushed_at="2026-01-01T00:00:00Z"),
        repository_payload(5, description="AI tool"),
        repository_payload(
            6,
            full_name="example/log-collector",
            description="Production observability utilities for distributed systems",
            topics=["developer-tools", "logging"],
        ),
        repository_payload(7, description=None),
        repository_payload(
            8,
            full_name="example/user-agent-parser",
            description="Cross-platform user-agent parser for web analytics systems",
            topics=["browser", "parser"],
        ),
        repository_payload(
            9,
            full_name="example/undocumented-ai-runtime",
            description="AI inference runtime with a placeholder README document",
        ),
    ]
    readme_requests: list[httpx.Request] = []

    def readme_handler(request: httpx.Request) -> httpx.Response:
        readme_requests.append(request)
        full_name = request.url.path.removeprefix("/repos/").removesuffix(
            "/readme"
        )
        size = 100 if full_name == "example/undocumented-ai-runtime" else 1_200
        return httpx.Response(200, json=readme_payload(full_name, size=size))

    application = create_github_trends_application(
        GitHubTrendsSettings(state_path=tmp_path / "state.json"),
        clock=lambda: NOW,
        http_client_factory=client_factory(
            lambda request: httpx.Response(
                200,
                json={
                    "total_count": len(items),
                    "incomplete_results": False,
                    "items": items,
                },
            ),
            readme_handler=readme_handler,
        ),
    )

    result = await application.run()

    assert result.repository_count == 1
    assert [request.url.path for request in readme_requests] == [
        "/repos/example/inference-1/readme",
        "/repos/example/undocumented-ai-runtime/readme",
    ]
    state = GitHubTrendsState.load(tmp_path / "state.json")
    assert set(state.baseline_repositories) == {"1"}
    snapshot = state.baseline_repositories["1"]
    assert snapshot.full_name == "example/inference-1"
    assert str(snapshot.url) == "https://github.com/example/inference-1"
    assert snapshot.created_at == datetime(2026, 6, 1, tzinfo=UTC)
    assert snapshot.stars == 100
    assert snapshot.language == "Python"
    assert snapshot.pushed_at == datetime(2026, 9, 9, 12, tzinfo=UTC)
    assert snapshot.description == "AI inference runtime for production model serving"
    assert snapshot.topics == ("artificial-intelligence", "inference")
    assert state.baseline is not None
    assert state.baseline.sampled_at == NOW
    assert snapshot.readme_size == 1_200
    assert str(snapshot.readme_url).endswith("/blob/main/README.md")


@pytest.mark.asyncio
async def test_github_trends_entry_updates_latest_snapshot_without_moving_baseline(
    tmp_path,
) -> None:
    state_path = tmp_path / "state.json"
    current_time = [NOW]
    current_stars = [100]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [repository_payload(1, stars=current_stars[0])],
            },
        )

    def application():
        return create_github_trends_application(
            GitHubTrendsSettings(state_path=state_path),
            clock=lambda: current_time[0],
            http_client_factory=client_factory(handler),
        )

    assert (await application().run()).status is RunStatus.BASELINE_ESTABLISHED
    current_time[0] = NOW + timedelta(days=1)
    current_stars[0] = 125

    result = await application().run()

    assert result.status is RunStatus.SNAPSHOT_UPDATED
    state = GitHubTrendsState.load(state_path)
    assert state.baseline_at == NOW
    assert state.baseline_repositories["1"].stars == 100
    assert state.latest_at == NOW + timedelta(days=1)
    assert state.latest_repositories["1"].stars == 125


@pytest.mark.asyncio
async def test_github_trends_entry_sends_at_exactly_seventy_two_hours_and_advances_baseline(
    tmp_path,
) -> None:
    state_path = tmp_path / "state.json"
    current_time = [NOW]
    current_stars = [100]
    sent: list[tuple[list[str], str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "model.example":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "recommendations": [
                                            {
                                                "repository_id": 1,
                                                "purpose": "用于部署和管理生产环境中的 AI 推理服务。",
                                                "reason": "最近七十二小时 Star 增长明显，项目文档与活动状态完整。",
                                            }
                                        ]
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [repository_payload(1, stars=current_stars[0])],
            },
        )

    class SenderSpy:
        async def send(self, parts, title) -> None:
            sent.append((list(parts), title))

    settings = GitHubTrendsSettings(
        github_token="test-github-token",
        state_path=state_path,
        ai_api_key="test-ai-key",
        ai_base_url="https://model.example/v1",
        ai_model="gpt-5.6-luna",
        dingtalk_webhook=(
            "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        ),
    )

    def application():
        return create_github_trends_application(
            settings,
            clock=lambda: current_time[0],
            http_client_factory=client_factory(handler),
            sender_factory=lambda client: SenderSpy(),
        )

    assert (await application().run()).status is RunStatus.BASELINE_ESTABLISHED
    current_time[0] = NOW + timedelta(hours=72)
    current_stars[0] = 180

    result = await application().run()

    assert result.status is RunStatus.SENT
    assert result.repository_count == 1
    assert result.part_count == 1
    assert sent[0][1] == "GitHub AI 趋势报告｜2026-09-13"
    assert "example/inference-1" in sent[0][0][0]
    assert "72 小时新增 Star：80" in sent[0][0][0]
    state = GitHubTrendsState.load(state_path)
    assert state.baseline_at == NOW + timedelta(hours=72)
    assert state.baseline_repositories["1"].stars == 180
    assert state.last_successful_report_at == NOW + timedelta(hours=72)


@pytest.mark.asyncio
async def test_github_trends_entry_does_not_send_one_second_before_seventy_two_hours(
    tmp_path,
) -> None:
    state_path = tmp_path / "state.json"
    current_time = [NOW]
    current_stars = [100]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "model.example":
            raise AssertionError("model must not be called before 72 hours")
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [repository_payload(1, stars=current_stars[0])],
            },
        )

    settings = GitHubTrendsSettings(
        state_path=state_path,
        ai_api_key="test-ai-key",
        ai_base_url="https://model.example/v1",
        dingtalk_webhook=(
            "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        ),
    )

    def application():
        return create_github_trends_application(
            settings,
            clock=lambda: current_time[0],
            http_client_factory=client_factory(handler),
            sender_factory=lambda client: (_ for _ in ()).throw(
                AssertionError("sender must not be constructed before 72 hours")
            ),
        )

    assert (await application().run()).status is RunStatus.BASELINE_ESTABLISHED
    current_time[0] = NOW + timedelta(hours=72) - timedelta(seconds=1)
    current_stars[0] = 179

    result = await application().run()

    assert result.status is RunStatus.SNAPSHOT_UPDATED
    state = GitHubTrendsState.load(state_path)
    assert state.baseline_at == NOW
    assert state.baseline_repositories["1"].stars == 100
    assert state.latest_at == current_time[0]
    assert state.last_successful_report_at is None


@pytest.mark.asyncio
async def test_github_trends_entry_sends_fewer_items_without_losing_emerging_category(
    tmp_path,
) -> None:
    state_path = tmp_path / "state.json"
    current_time = [NOW]
    star_counts = [{1: 100, 2: 10}]
    repositories = [
        repository_payload(
            1,
            full_name="example/mature-inference",
            created_at="2025-01-01T00:00:00Z",
            stars=star_counts[0][1],
        ),
        repository_payload(
            2,
            full_name="example/new-agent",
            created_at="2026-09-01T00:00:00Z",
            stars=star_counts[0][2],
        ),
    ]
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "model.example":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "recommendations": [
                                            {
                                                "repository_id": repository_id,
                                                "purpose": "用于构建可部署且有文档支持的人工智能工具。",
                                                "reason": "项目与人工智能直接相关，并保持了清晰说明和近期活动。",
                                            }
                                            for repository_id in (1, 2)
                                        ]
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )
        current = [
            item | {"stargazers_count": star_counts[0][int(item["id"])]}
            for item in repositories
        ]
        return httpx.Response(
            200,
            json={
                "total_count": 2,
                "incomplete_results": False,
                "items": current,
            },
        )

    class SenderSpy:
        async def send(self, parts, title) -> None:
            sent.extend(parts)

    settings = GitHubTrendsSettings(
        state_path=state_path,
        ai_api_key="test-ai-key",
        ai_base_url="https://model.example/v1",
        dingtalk_webhook=(
            "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        ),
    )

    def application():
        return create_github_trends_application(
            settings,
            clock=lambda: current_time[0],
            http_client_factory=client_factory(handler),
            sender_factory=lambda client: SenderSpy(),
        )

    assert (await application().run()).status is RunStatus.BASELINE_ESTABLISHED
    current_time[0] += timedelta(hours=72)
    star_counts[0] = {1: 120, 2: 90}

    result = await application().run()

    assert result.status is RunStatus.SENT
    assert result.repository_count == 2
    assert sent[0].count("### ") == 2
    assert "## 增长动量项目" in sent[0]
    assert "example\\/mature\\-inference" in sent[0]
    assert "## 新兴项目" in sent[0]
    assert "example\\/new\\-agent" in sent[0]


@pytest.mark.asyncio
async def test_github_trends_dry_run_with_empty_cache_does_not_persist_a_baseline(
    tmp_path,
) -> None:
    state_path = tmp_path / "state.json"
    application = create_github_trends_application(
        GitHubTrendsSettings(state_path=state_path, dry_run=True),
        clock=lambda: NOW,
        http_client_factory=client_factory(
            lambda request: httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "incomplete_results": False,
                    "items": [repository_payload(1, stars=100)],
                },
            )
        ),
        sender_factory=lambda client: (_ for _ in ()).throw(
            AssertionError("dry-run must not construct a sender")
        ),
    )

    result = await application.run()

    assert result.status is RunStatus.PREVIEW
    assert result.repository_count == 1
    assert result.part_count == 0
    assert not state_path.exists()


@pytest.mark.asyncio
async def test_github_trends_entry_limits_report_to_three_momentum_and_two_emerging(
    tmp_path,
) -> None:
    state_path = tmp_path / "state.json"
    current_time = [NOW]
    baseline_stars = {index: 100 for index in range(1, 5)} | {
        index: 10 for index in range(5, 8)
    }
    current_stars = [baseline_stars]
    repositories = [
        repository_payload(
            index,
            stars=baseline_stars[index],
            created_at=(
                "2025-01-01T00:00:00Z"
                if index <= 4
                else "2026-09-01T00:00:00Z"
            ),
        )
        for index in range(1, 8)
    ]
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "model.example":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "recommendations": [
                                            {
                                                "repository_id": index,
                                                "purpose": "用于构建具有完整文档的生产级人工智能开发工具。",
                                                "reason": "项目与人工智能直接相关，近期活跃且观察期增长可验证。",
                                            }
                                            for index in range(1, 8)
                                        ]
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "total_count": 7,
                "incomplete_results": False,
                "items": [
                    item
                    | {
                        "stargazers_count": current_stars[0][int(item["id"])]
                    }
                    for item in repositories
                ],
            },
        )

    class SenderSpy:
        async def send(self, parts, title) -> None:
            sent.extend(parts)

    settings = GitHubTrendsSettings(
        state_path=state_path,
        ai_api_key="test-ai-key",
        ai_base_url="https://model.example/v1",
        dingtalk_webhook=(
            "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        ),
    )

    def application():
        return create_github_trends_application(
            settings,
            clock=lambda: current_time[0],
            http_client_factory=client_factory(handler),
            sender_factory=lambda client: SenderSpy(),
        )

    assert (await application().run()).status is RunStatus.BASELINE_ESTABLISHED
    current_time[0] += timedelta(hours=72)
    current_stars[0] = {
        1: 200,
        2: 190,
        3: 180,
        4: 170,
        5: 70,
        6: 60,
        7: 50,
    }

    result = await application().run()

    assert result.status is RunStatus.SENT
    assert result.repository_count == 5
    text = sent[0]
    assert text.count("### ") == 5
    assert [
        name in text
        for name in (
            "example\\/inference\\-1",
            "example\\/inference\\-2",
            "example\\/inference\\-3",
            "example\\/inference\\-5",
            "example\\/inference\\-6",
        )
    ] == [True] * 5
    assert "example\\/inference\\-4" not in text
    assert "example\\/inference\\-7" not in text
    assert "用途：" in text
    assert "入选原因：" in text
    assert "当前 Star：" in text
    assert "72 小时新增 Star：" in text
    assert "主要语言：Python" in text
    assert "https://github.com/example/inference-1" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("recommended_ids", [[999], [1, 1]])
async def test_github_trends_model_cannot_select_unknown_or_duplicate_repositories(
    tmp_path,
    recommended_ids: list[int],
) -> None:
    state_path = tmp_path / "state.json"
    current_time = [NOW]
    current_stars = [100]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "model.example":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "recommendations": [
                                            {
                                                "repository_id": repository_id,
                                                "purpose": "用于构建具有完整文档的人工智能推理工具。",
                                                "reason": "候选项目近期活跃且具有可核验的增长数据。",
                                            }
                                            for repository_id in recommended_ids
                                        ]
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [repository_payload(1, stars=current_stars[0])],
            },
        )

    settings = GitHubTrendsSettings(
        state_path=state_path,
        ai_api_key="test-ai-key",
        ai_base_url="https://model.example/v1",
        dingtalk_webhook=(
            "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        ),
    )

    def application():
        return create_github_trends_application(
            settings,
            clock=lambda: current_time[0],
            http_client_factory=client_factory(handler),
            sender_factory=lambda client: (_ for _ in ()).throw(
                AssertionError("invalid model output must not construct a sender")
            ),
        )

    assert (await application().run()).status is RunStatus.BASELINE_ESTABLISHED
    original_state = state_path.read_bytes()
    current_time[0] += timedelta(hours=72)
    current_stars[0] = 150

    result = await application().run()

    assert result.status is RunStatus.FAILED
    assert result.failure_type == "GitHubTrendAnalysisError"
    assert state_path.read_bytes() == original_state


@pytest.mark.asyncio
async def test_github_trends_model_retries_transient_failures_before_sending(
    tmp_path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "state.json"
    current_time = [NOW]
    current_stars = [100]
    model_responses = [503, 429, 200]
    model_calls = 0
    delays: list[int] = []
    sent: list[str] = []

    async def record_delay(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr(
        "ai_daily.github_trends_report.asyncio.sleep", record_delay
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal model_calls
        if request.url.host == "model.example":
            status = model_responses[model_calls]
            model_calls += 1
            if status != 200:
                return httpx.Response(status, text="private upstream detail")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "recommendations": [
                                            {
                                                "repository_id": 1,
                                                "purpose": "用于生产环境中的人工智能推理服务部署。",
                                                "reason": "项目与人工智能直接相关且增长与活动均可验证。",
                                            }
                                        ]
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [repository_payload(1, stars=current_stars[0])],
            },
        )

    class SenderSpy:
        async def send(self, parts, title) -> None:
            sent.extend(parts)

    settings = GitHubTrendsSettings(
        state_path=state_path,
        ai_api_key="test-ai-key",
        ai_base_url="https://model.example/v1",
        dingtalk_webhook=(
            "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        ),
    )

    def application():
        return create_github_trends_application(
            settings,
            clock=lambda: current_time[0],
            http_client_factory=client_factory(handler),
            sender_factory=lambda client: SenderSpy(),
        )

    assert (await application().run()).status is RunStatus.BASELINE_ESTABLISHED
    current_time[0] += timedelta(hours=72)
    current_stars[0] = 140

    result = await application().run()

    assert result.status is RunStatus.SENT
    assert model_calls == 3
    assert delays == [1, 2]
    assert len(sent) == 1
    assert (
        GitHubTrendsState.load(state_path).last_successful_report_at
        == current_time[0]
    )


@pytest.mark.asyncio
async def test_github_trends_delivery_failure_preserves_baseline_for_next_retry(
    tmp_path,
) -> None:
    state_path = tmp_path / "state.json"
    ai_digest_state_path = tmp_path / "ai-digest-deliveries.json"
    ai_digest_state_path.write_text(
        '{"2026-09-13":"unchanged"}\n', encoding="utf-8"
    )
    original_ai_digest_state = ai_digest_state_path.read_bytes()
    current_time = [NOW]
    current_stars = [100]
    successful_sends: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "model.example":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "recommendations": [
                                            {
                                                "repository_id": 1,
                                                "purpose": "用于生产环境中的人工智能推理服务部署。",
                                                "reason": "项目与人工智能直接相关且增长与活动均可验证。",
                                            }
                                        ]
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [repository_payload(1, stars=current_stars[0])],
            },
        )

    class FailingSender:
        async def send(self, parts, title) -> None:
            raise DingTalkError("safe delivery failure")

    class SuccessfulSender:
        async def send(self, parts, title) -> None:
            successful_sends.extend(parts)

    settings = GitHubTrendsSettings(
        state_path=state_path,
        ai_api_key="test-ai-key",
        ai_base_url="https://model.example/v1",
        dingtalk_webhook=(
            "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        ),
    )

    def application(sender):
        return create_github_trends_application(
            settings,
            clock=lambda: current_time[0],
            http_client_factory=client_factory(handler),
            sender_factory=lambda client: sender,
        )

    assert (
        await application(SuccessfulSender()).run()
    ).status is RunStatus.BASELINE_ESTABLISHED
    original_state = state_path.read_bytes()
    current_time[0] += timedelta(hours=72)
    current_stars[0] = 160

    failed = await application(FailingSender()).run()

    assert failed.status is RunStatus.FAILED
    assert failed.failure_type == "DingTalkError"
    assert state_path.read_bytes() == original_state
    assert ai_digest_state_path.read_bytes() == original_ai_digest_state

    current_time[0] += timedelta(hours=24)
    current_stars[0] = 180
    retried = await application(SuccessfulSender()).run()

    assert retried.status is RunStatus.SENT
    assert len(successful_sends) == 1
    assert "96 小时新增 Star：80" in successful_sends[0]
    state = GitHubTrendsState.load(state_path)
    assert state.last_successful_report_at == current_time[0]
    assert state.baseline_repositories["1"].stars == 180
    assert ai_digest_state_path.read_bytes() == original_ai_digest_state


@pytest.mark.asyncio
async def test_due_github_trends_dry_run_previews_without_advancing_state(
    tmp_path,
    capsys,
) -> None:
    state_path = tmp_path / "state.json"
    current_time = [NOW]
    current_stars = [100]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "model.example":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "recommendations": [
                                            {
                                                "repository_id": 1,
                                                "purpose": "用于生产环境中的人工智能推理服务部署。",
                                                "reason": "项目与人工智能直接相关且增长与活动均可验证。",
                                            }
                                        ]
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [repository_payload(1, stars=current_stars[0])],
            },
        )

    live_settings = GitHubTrendsSettings(state_path=state_path)
    baseline = create_github_trends_application(
        live_settings,
        clock=lambda: current_time[0],
        http_client_factory=client_factory(handler),
    )
    assert (await baseline.run()).status is RunStatus.BASELINE_ESTABLISHED
    original_state = state_path.read_bytes()

    current_time[0] += timedelta(hours=72)
    current_stars[0] = 170
    preview = create_github_trends_application(
        GitHubTrendsSettings(
            state_path=state_path,
            ai_api_key="test-ai-key",
            ai_base_url="https://model.example/v1",
            dry_run=True,
        ),
        clock=lambda: current_time[0],
        http_client_factory=client_factory(handler),
        sender_factory=lambda client: (_ for _ in ()).throw(
            AssertionError("dry-run must not construct a sender")
        ),
    )

    result = await preview.run()

    assert result.status is RunStatus.PREVIEW
    assert result.repository_count == 1
    assert result.part_count == 1
    assert "GitHub AI 趋势报告" in capsys.readouterr().out
    assert state_path.read_bytes() == original_state


@pytest.mark.asyncio
async def test_following_github_trend_period_starts_at_last_successful_report(
    tmp_path,
) -> None:
    state_path = tmp_path / "state.json"
    current_time = [NOW]
    current_stars = [100]
    model_calls = 0
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal model_calls
        if request.url.host == "model.example":
            model_calls += 1
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "recommendations": [
                                            {
                                                "repository_id": 1,
                                                "purpose": "用于生产环境中的人工智能推理服务部署。",
                                                "reason": "项目与人工智能直接相关且增长与活动均可验证。",
                                            }
                                        ]
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [repository_payload(1, stars=current_stars[0])],
            },
        )

    class SenderSpy:
        async def send(self, parts, title) -> None:
            sent.extend(parts)

    settings = GitHubTrendsSettings(
        state_path=state_path,
        ai_api_key="test-ai-key",
        ai_base_url="https://model.example/v1",
        dingtalk_webhook=(
            "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        ),
    )

    def application():
        return create_github_trends_application(
            settings,
            clock=lambda: current_time[0],
            http_client_factory=client_factory(handler),
            sender_factory=lambda client: SenderSpy(),
        )

    assert (await application().run()).status is RunStatus.BASELINE_ESTABLISHED
    current_time[0] += timedelta(hours=72)
    current_stars[0] = 150
    assert (await application().run()).status is RunStatus.SENT

    current_time[0] = NOW + timedelta(hours=144) - timedelta(seconds=1)
    current_stars[0] = 170
    before_boundary = await application().run()

    assert before_boundary.status is RunStatus.SNAPSHOT_UPDATED
    assert model_calls == 1
    state = GitHubTrendsState.load(state_path)
    assert state.baseline_repositories["1"].stars == 150
    assert state.last_successful_report_at == NOW + timedelta(hours=72)

    current_time[0] = NOW + timedelta(hours=144)
    current_stars[0] = 180
    at_boundary = await application().run()

    assert at_boundary.status is RunStatus.SENT
    assert model_calls == 2
    assert len(sent) == 2
    assert "72 小时新增 Star：50" in sent[0]
    assert "72 小时新增 Star：30" in sent[1]
    state = GitHubTrendsState.load(state_path)
    assert state.baseline_repositories["1"].stars == 180
    assert state.last_successful_report_at == NOW + timedelta(hours=144)


@pytest.mark.asyncio
async def test_github_trends_does_not_fill_report_when_quality_gate_rejects_candidates(
    tmp_path,
) -> None:
    state_path = tmp_path / "state.json"
    current_time = [NOW]
    current_stars = [100]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "model.example":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"recommendations": []}, ensure_ascii=False
                                )
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [repository_payload(1, stars=current_stars[0])],
            },
        )

    settings = GitHubTrendsSettings(
        state_path=state_path,
        ai_api_key="test-ai-key",
        ai_base_url="https://model.example/v1",
        dingtalk_webhook=(
            "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        ),
    )

    def application():
        return create_github_trends_application(
            settings,
            clock=lambda: current_time[0],
            http_client_factory=client_factory(handler),
            sender_factory=lambda client: (_ for _ in ()).throw(
                AssertionError("an empty quality result must stay silent")
            ),
        )

    assert (await application().run()).status is RunStatus.BASELINE_ESTABLISHED
    current_time[0] += timedelta(hours=72)
    current_stars[0] = 1_000

    result = await application().run()

    assert result.status is RunStatus.EMPTY
    assert result.part_count == 0
    state = GitHubTrendsState.load(state_path)
    assert state.baseline_at == NOW
    assert state.baseline_repositories["1"].stars == 100
    assert state.last_successful_report_at is None


@pytest.mark.asyncio
async def test_repository_created_during_observation_period_uses_zero_star_baseline(
    tmp_path,
) -> None:
    state_path = tmp_path / "state.json"
    current_time = [NOW]
    due = [False]
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "model.example":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "recommendations": [
                                            {
                                                "repository_id": 2,
                                                "purpose": "用于构建近期出现的人工智能代理应用。",
                                                "reason": "项目在观察期内创建并获得可验证的快速增长。",
                                            }
                                        ]
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )
        items = [
            repository_payload(
                1,
                stars=100,
                created_at="2025-01-01T00:00:00Z",
            )
        ]
        if due[0]:
            items.append(
                repository_payload(
                    2,
                    stars=50,
                    created_at="2026-09-11T00:45:00Z",
                )
            )
        return httpx.Response(
            200,
            json={
                "total_count": len(items),
                "incomplete_results": False,
                "items": items,
            },
        )

    class SenderSpy:
        async def send(self, parts, title) -> None:
            sent.extend(parts)

    settings = GitHubTrendsSettings(
        state_path=state_path,
        ai_api_key="test-ai-key",
        ai_base_url="https://model.example/v1",
        dingtalk_webhook=(
            "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        ),
    )

    def application():
        return create_github_trends_application(
            settings,
            clock=lambda: current_time[0],
            http_client_factory=client_factory(handler),
            sender_factory=lambda client: SenderSpy(),
        )

    assert (await application().run()).status is RunStatus.BASELINE_ESTABLISHED
    current_time[0] += timedelta(hours=72)
    due[0] = True

    result = await application().run()

    assert result.status is RunStatus.SENT
    assert result.repository_count == 1
    assert "## 新兴项目" in sent[0]
    assert "example\\/inference\\-2" in sent[0]
    assert "72 小时新增 Star：50" in sent[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_state",
    [
        None,
        "not-json",
        json.dumps({"schema_version": 999}),
    ],
)
async def test_github_trends_entry_rebuilds_missing_or_invalid_cache(
    tmp_path,
    invalid_state: str | None,
) -> None:
    state_path = tmp_path / "state.json"
    if invalid_state is not None:
        state_path.write_text(invalid_state, encoding="utf-8")
    application = create_github_trends_application(
        GitHubTrendsSettings(state_path=state_path),
        clock=lambda: NOW,
        http_client_factory=client_factory(
            lambda request: httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "incomplete_results": False,
                    "items": [repository_payload(1, stars=777)],
                },
            )
        ),
    )

    result = await application.run()

    assert result.status is RunStatus.BASELINE_ESTABLISHED
    state = GitHubTrendsState.load(state_path)
    assert state.baseline_at == NOW
    assert state.baseline_repositories["1"].stars == 777
    assert state.latest_repositories["1"].stars == 777


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="private upstream response"),
        httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": True,
                "items": [repository_payload(1, stars=999)],
            },
        ),
        httpx.Response(
            200,
            json={
                "total_count": 2,
                "incomplete_results": False,
                "items": [repository_payload(1, stars=999)],
            },
        ),
    ],
)
async def test_github_trends_entry_fails_without_overwriting_valid_state(
    tmp_path,
    response: httpx.Response,
    monkeypatch,
) -> None:
    async def no_delay(delay: int) -> None:
        pass

    monkeypatch.setattr("ai_daily.github_api.asyncio.sleep", no_delay)
    state_path = tmp_path / "state.json"
    baseline_application = create_github_trends_application(
        GitHubTrendsSettings(state_path=state_path),
        clock=lambda: NOW,
        http_client_factory=client_factory(
            lambda request: httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "incomplete_results": False,
                    "items": [repository_payload(1, stars=100)],
                },
            )
        ),
    )
    await baseline_application.run()
    original_state = state_path.read_bytes()
    failing_application = create_github_trends_application(
        GitHubTrendsSettings(state_path=state_path),
        clock=lambda: NOW + timedelta(days=1),
        http_client_factory=client_factory(lambda request: response),
    )

    result = await failing_application.run()

    assert result.status is RunStatus.FAILED
    assert result.failure_type == "GitHubAPIError"
    assert result.repository_count == 0
    assert state_path.read_bytes() == original_state


@pytest.mark.asyncio
async def test_github_readme_failure_does_not_overwrite_valid_state(
    tmp_path,
    monkeypatch,
) -> None:
    async def no_delay(delay: int) -> None:
        pass

    monkeypatch.setattr("ai_daily.github_api.asyncio.sleep", no_delay)
    state_path = tmp_path / "state.json"
    search_response = lambda request: httpx.Response(
        200,
        json={
            "total_count": 1,
            "incomplete_results": False,
            "items": [repository_payload(1)],
        },
    )
    baseline = create_github_trends_application(
        GitHubTrendsSettings(state_path=state_path),
        clock=lambda: NOW,
        http_client_factory=client_factory(search_response),
    )
    assert (await baseline.run()).status is RunStatus.BASELINE_ESTABLISHED
    original_state = state_path.read_bytes()

    failing = create_github_trends_application(
        GitHubTrendsSettings(state_path=state_path),
        clock=lambda: NOW + timedelta(days=1),
        http_client_factory=client_factory(
            search_response,
            readme_handler=lambda request: httpx.Response(
                503, text="private README API response"
            ),
        ),
    )

    result = await failing.run()

    assert result.status is RunStatus.FAILED
    assert result.failure_type == "GitHubAPIError"
    assert state_path.read_bytes() == original_state


def test_github_trends_command_prints_its_independent_result(
    tmp_path, monkeypatch, capsys
) -> None:
    from ai_daily import github_trends_cli

    configured = GitHubTrendsSettings(state_path=tmp_path / "state.json")
    monkeypatch.setattr(github_trends_cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(
        github_trends_cli,
        "load_github_trends_settings",
        lambda: configured,
    )

    async def run(settings):
        assert settings is configured
        return GitHubTrendsRunResult(
            status=RunStatus.BASELINE_ESTABLISHED,
            repository_count=4,
            part_count=0,
        )

    monkeypatch.setattr(github_trends_cli, "run_github_trends", run)

    assert github_trends_cli.main() == 0
    assert capsys.readouterr().out == (
        "status=baseline-established repositories=4 parts=0\n"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["created_at", "pushed_at"])
async def test_github_api_contract_rejects_timezone_naive_repository_times(
    field: str,
) -> None:
    item = repository_payload(1)
    item[field] = "2026-09-09T12:00:00"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "incomplete_results": False,
                    "items": [item],
                },
            )
        )
    ) as client:
        with pytest.raises(GitHubAPIError, match="response is invalid"):
            await GitHubRepositorySearchClient(client, None).snapshot(NOW)


@pytest.mark.asyncio
async def test_github_api_contract_hides_token_and_response_body_on_rate_limit(
) -> None:
    token = "never-log-this-github-token"
    body = "private upstream response with repository details"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, text=f"{body} {token}")
        )
    ) as client:
        with pytest.raises(GitHubAPIError) as captured:
            await GitHubRepositorySearchClient(client, token).snapshot(NOW)

    message = str(captured.value)
    assert message == "GitHub search request failed with HTTP 429"
    assert token not in message
    assert body not in message


@pytest.mark.asyncio
async def test_github_api_retries_transient_search_and_readme_failures(
    monkeypatch,
) -> None:
    calls = {"search": 0, "readme": 0}
    delays: list[int] = []

    async def record_delay(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr("ai_daily.github_api.asyncio.sleep", record_delay)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/repositories":
            calls["search"] += 1
            if calls["search"] == 1:
                return httpx.Response(503, text="private search response")
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "incomplete_results": False,
                    "items": [repository_payload(1)],
                },
            )
        if request.url.path == "/repos/example/inference-1/readme":
            calls["readme"] += 1
            if calls["readme"] == 1:
                return httpx.Response(429, text="private README response")
            return httpx.Response(
                200,
                json=readme_payload("example/inference-1"),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        snapshots = await GitHubRepositorySearchClient(
            client, "test-github-token"
        ).snapshot(NOW)

    assert [snapshot.repository_id for snapshot in snapshots] == [1]
    assert calls == {"search": 2, "readme": 2}
    assert delays == [1, 1]


def test_github_trends_command_returns_failure_without_upstream_details(
    tmp_path, monkeypatch, capsys, caplog
) -> None:
    from ai_daily import github_trends_cli

    configured = GitHubTrendsSettings(state_path=tmp_path / "state.json")
    monkeypatch.setattr(github_trends_cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(
        github_trends_cli,
        "load_github_trends_settings",
        lambda: configured,
    )

    async def run(settings):
        assert settings is configured
        return GitHubTrendsRunResult(
            status=RunStatus.FAILED,
            repository_count=0,
            part_count=0,
            failure_type="GitHubAPIError",
        )

    monkeypatch.setattr(github_trends_cli, "run_github_trends", run)

    assert github_trends_cli.main() == 1
    assert capsys.readouterr().out == "status=failed repositories=0 parts=0\n"
    assert "GitHubAPIError" in caplog.text
    assert "upstream" not in caplog.text
