from datetime import UTC, datetime

import httpx
import pytest

from ai_daily.application import RunStatus
from ai_daily.github_trends_app import (
    GitHubTrendsApplication,
    GitHubTrendsContext,
    GitHubTrendsPreparation,
    GitHubTrendsReport,
    GitHubTrendsRunResult,
    GitHubTrendsRuntime,
    GitHubTrendsState,
)
from ai_daily.github_trends_state import GitHubCandidateSnapshot


NOW = datetime(2026, 7, 18, 0, 30, tzinfo=UTC)


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


@pytest.mark.asyncio
async def test_github_trends_entry_uses_its_own_runtime_and_state() -> None:
    initial_state = GitHubTrendsState()
    updated_state = GitHubTrendsState(
        latest=GitHubCandidateSnapshot(sampled_at=NOW)
    )
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
    baseline = GitHubTrendsState(
        latest=GitHubCandidateSnapshot(sampled_at=NOW)
    )
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
