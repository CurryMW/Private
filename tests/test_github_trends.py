import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ai_daily.application import RunStatus
from ai_daily.github_api import GitHubAPIError, GitHubRepositorySearchClient
from ai_daily.github_trends import (
    GitHubTrendsSettings,
    create_github_trends_application,
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
        "created_at": "2026-06-01T00:00:00Z",
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
) -> None:
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
) -> None:
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
