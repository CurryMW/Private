from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from ai_daily.application import (
    HTTPClientFactory,
    MessageSender,
    RunStatus,
    StateStore,
)
from ai_daily.github_trends_state import GitHubTrendsState


@dataclass(frozen=True)
class GitHubTrendsRunResult:
    status: RunStatus
    repository_count: int
    part_count: int
    failure_type: str | None = None
    _failure: Exception | None = field(default=None, compare=False, repr=False)

    @classmethod
    def failed(cls, error: Exception) -> "GitHubTrendsRunResult":
        return cls(
            status=RunStatus.FAILED,
            repository_count=0,
            part_count=0,
            failure_type=type(error).__name__,
            _failure=error,
        )


@dataclass(frozen=True)
class GitHubTrendsReport:
    title: str
    parts: tuple[str, ...]
    repository_count: int


@dataclass(frozen=True)
class GitHubTrendsPreparation:
    next_state: GitHubTrendsState
    report: GitHubTrendsReport | None
    repository_count: int
    no_report_status: RunStatus = RunStatus.EMPTY


@dataclass(frozen=True)
class GitHubTrendsContext:
    run_at: datetime
    http_client: httpx.AsyncClient
    state: GitHubTrendsState


@dataclass(frozen=True)
class GitHubTrendsRuntime:
    clock: Callable[[], datetime]
    http_client_factory: HTTPClientFactory
    state_store: StateStore[GitHubTrendsState]
    sender_factory: Callable[[httpx.AsyncClient], MessageSender]
    dry_run: bool = False
    preview_writer: Callable[[str], None] = print


PrepareGitHubTrendsReport = Callable[
    [GitHubTrendsContext], Awaitable[GitHubTrendsPreparation]
]


class GitHubTrendsApplication:
    def __init__(
        self,
        prepare_report: PrepareGitHubTrendsReport,
        runtime: GitHubTrendsRuntime,
    ) -> None:
        self._prepare_report = prepare_report
        self._runtime = runtime

    async def run(self) -> GitHubTrendsRunResult:
        try:
            run_at = self._runtime.clock()
            if run_at.tzinfo is None or run_at.utcoffset() is None:
                raise ValueError("clock must return a timezone-aware timestamp")
            run_at = run_at.astimezone(UTC)
            state = self._runtime.state_store.load()
            async with self._runtime.http_client_factory() as client:
                context = GitHubTrendsContext(
                    run_at=run_at,
                    http_client=client,
                    state=state,
                )
                preparation = await self._prepare_report(context)
                report = preparation.report
                if report is None:
                    if self._runtime.dry_run:
                        return GitHubTrendsRunResult(
                            status=RunStatus.PREVIEW,
                            repository_count=preparation.repository_count,
                            part_count=0,
                        )
                    self._runtime.state_store.save(preparation.next_state)
                    return GitHubTrendsRunResult(
                        status=preparation.no_report_status,
                        repository_count=preparation.repository_count,
                        part_count=0,
                    )

                if self._runtime.dry_run:
                    for index, part in enumerate(report.parts, 1):
                        self._runtime.preview_writer(
                            f"--- preview {index}/{len(report.parts)} ---"
                        )
                        self._runtime.preview_writer(part)
                    return GitHubTrendsRunResult(
                        status=RunStatus.PREVIEW,
                        repository_count=report.repository_count,
                        part_count=len(report.parts),
                    )

                await self._runtime.sender_factory(client).send(
                    report.parts, report.title
                )
                self._runtime.state_store.save(preparation.next_state)
                return GitHubTrendsRunResult(
                    status=RunStatus.SENT,
                    repository_count=report.repository_count,
                    part_count=len(report.parts),
                )
        except Exception as error:
            return GitHubTrendsRunResult.failed(error)
