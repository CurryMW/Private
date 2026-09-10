from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, SecretStr

from ai_daily.application import HTTPClientFactory, MessageSender, RunStatus
from ai_daily.github_api import GitHubRepositorySearchClient
from ai_daily.github_trends_app import (
    GitHubTrendsApplication,
    GitHubTrendsContext,
    GitHubTrendsPreparation,
    GitHubTrendsRunResult,
    GitHubTrendsRuntime,
)
from ai_daily.github_trends_state import (
    GitHubCandidateSnapshot,
    GitHubTrendsState,
    GitHubTrendsStateFileStore,
)


class GitHubTrendsSettings(BaseModel):
    github_token: SecretStr | None = None
    state_path: Path = Path(".state/github-trends/baseline.json")


def load_github_trends_settings(
    env: Mapping[str, str] | None = None,
) -> GitHubTrendsSettings:
    source = os.environ if env is None else env
    token = source.get("GITHUB_TOKEN")
    return GitHubTrendsSettings(
        github_token=token if token is not None and token.strip() else None,
        state_path=source.get(
            "GITHUB_TRENDS_STATE_PATH",
            ".state/github-trends/baseline.json",
        ),
    )


async def _prepare_snapshot(
    context: GitHubTrendsContext,
    token: str | None,
) -> GitHubTrendsPreparation:
    repositories = await GitHubRepositorySearchClient(
        context.http_client, token
    ).snapshot(context.run_at)
    by_id = {str(repository.repository_id): repository for repository in repositories}
    snapshot = GitHubCandidateSnapshot(
        sampled_at=context.run_at,
        repositories=by_id,
    )
    if not context.state.has_baseline:
        next_state = GitHubTrendsState(
            baseline=snapshot,
            latest=snapshot,
        )
        status = RunStatus.BASELINE_ESTABLISHED
    else:
        next_state = context.state.model_copy(
            update={"latest": snapshot}
        )
        status = RunStatus.SNAPSHOT_UPDATED
    return GitHubTrendsPreparation(
        next_state=next_state,
        report=None,
        repository_count=len(repositories),
        no_report_status=status,
    )


def _default_http_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": "dingtalk-ai-daily/0.1"}
    )


def _sender_is_not_available(_: httpx.AsyncClient) -> MessageSender:
    raise RuntimeError("GitHub trend delivery is not implemented")


def create_github_trends_application(
    settings: GitHubTrendsSettings,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    http_client_factory: HTTPClientFactory = _default_http_client_factory,
    sender_factory: Callable[
        [httpx.AsyncClient], MessageSender
    ] = _sender_is_not_available,
) -> GitHubTrendsApplication:
    token = (
        None
        if settings.github_token is None
        else settings.github_token.get_secret_value()
    )

    async def prepare(context: GitHubTrendsContext) -> GitHubTrendsPreparation:
        return await _prepare_snapshot(context, token)

    return GitHubTrendsApplication(
        prepare,
        GitHubTrendsRuntime(
            clock=clock,
            http_client_factory=http_client_factory,
            state_store=GitHubTrendsStateFileStore(settings.state_path),
            sender_factory=sender_factory,
        ),
    )


async def run_github_trends(
    settings: GitHubTrendsSettings,
) -> GitHubTrendsRunResult:
    return await create_github_trends_application(settings).run()
