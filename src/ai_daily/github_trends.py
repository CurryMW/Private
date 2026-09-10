from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, SecretStr, field_validator, model_validator

from ai_daily.application import HTTPClientFactory, MessageSender, RunStatus
from ai_daily.config import DEFAULT_AI_BASE_URL, PRODUCTION_MODEL, validate_ai_base_url
from ai_daily.dingtalk import DingTalkSender
from ai_daily.github_api import GitHubRepositorySearchClient
from ai_daily.github_trends_app import (
    GitHubTrendsApplication,
    GitHubTrendsContext,
    GitHubTrendsPreparation,
    GitHubTrendsReport,
    GitHubTrendsRunResult,
    GitHubTrendsRuntime,
)
from ai_daily.github_trends_state import (
    GitHubCandidateSnapshot,
    GitHubTrendsState,
    GitHubTrendsStateFileStore,
)
from ai_daily.github_trends_report import (
    GitHubTrendAnalyzer,
    GitHubTrendCandidate,
    GitHubTrendRecommendation,
    GitHubTrendReportItem,
    MAX_MODEL_CANDIDATES,
    render_github_trends_report,
)


REPORT_INTERVAL = timedelta(hours=72)
EMERGING_MAX_AGE = timedelta(days=90)
MOMENTUM_REPORT_LIMIT = 3
EMERGING_REPORT_LIMIT = 2
MOMENTUM_RECALL_LIMIT = 30
EMERGING_RECALL_LIMIT = MAX_MODEL_CANDIDATES - MOMENTUM_RECALL_LIMIT
EMERGING_RATIO_WEIGHT = 100.0


class GitHubTrendsSettings(BaseModel):
    github_token: SecretStr | None = None
    state_path: Path = Path(".state/github-trends/baseline.json")
    ai_api_key: SecretStr | None = None
    ai_base_url: str = DEFAULT_AI_BASE_URL
    ai_model: str = PRODUCTION_MODEL
    dingtalk_webhook: SecretStr | None = None
    dingtalk_access_token: SecretStr | None = None
    timezone: str = "Asia/Shanghai"
    dry_run: bool = False

    @field_validator("ai_base_url")
    @classmethod
    def validate_model_base_url(cls, value: str) -> str:
        return validate_ai_base_url(value)

    @field_validator("ai_model")
    @classmethod
    def validate_single_model(cls, value: str) -> str:
        model = value.strip()
        if model != PRODUCTION_MODEL:
            raise ValueError(f"AI_MODEL must be {PRODUCTION_MODEL}")
        return model

    @model_validator(mode="after")
    def validate_optional_webhook(self) -> "GitHubTrendsSettings":
        if self.dingtalk_webhook is None:
            return self
        parsed = urlsplit(self.dingtalk_webhook.get_secret_value())
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise ValueError("DINGTALK_WEBHOOK must be an HTTPS URL")
        has_embedded_token = bool(parse_qs(parsed.query).get("access_token"))
        has_separate_token = (
            self.dingtalk_access_token is not None
            and bool(self.dingtalk_access_token.get_secret_value().strip())
        )
        if not has_embedded_token and not has_separate_token:
            raise ValueError("DINGTALK_ACCESS_TOKEN is required for a base webhook")
        return self


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
        ai_api_key=_optional_secret(source, "AI_API_KEY"),
        ai_base_url=source.get("AI_BASE_URL", DEFAULT_AI_BASE_URL),
        ai_model=source.get("AI_MODEL", PRODUCTION_MODEL),
        dingtalk_webhook=_optional_secret(source, "DINGTALK_WEBHOOK"),
        dingtalk_access_token=_optional_secret(source, "DINGTALK_ACCESS_TOKEN"),
        timezone=source.get("TIMEZONE", "Asia/Shanghai"),
        dry_run=_parse_bool(source.get("DRY_RUN")),
    )


def _optional_secret(source: Mapping[str, str], name: str) -> str | None:
    value = source.get(name)
    return value if value is not None and value.strip() else None


def _parse_bool(value: str | None) -> bool:
    return value is not None and value.strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _report_credentials(settings: GitHubTrendsSettings) -> tuple[str, str]:
    if settings.ai_api_key is None:
        raise ValueError("AI_API_KEY is required for a GitHub trend report")
    if settings.dingtalk_webhook is None and not settings.dry_run:
        raise ValueError("DINGTALK_WEBHOOK is required for a GitHub trend report")
    return settings.ai_api_key.get_secret_value(), settings.ai_model


def _trend_candidates(
    state: GitHubTrendsState,
    snapshot: GitHubCandidateSnapshot,
    run_at: datetime,
) -> tuple[list[GitHubTrendCandidate], list[GitHubTrendCandidate]]:
    candidates: list[GitHubTrendCandidate] = []
    baseline_at = state.baseline_at
    if baseline_at is None:
        raise ValueError("GitHub trend baseline is invalid")
    for repository_id, repository in snapshot.repositories.items():
        baseline = state.baseline_repositories.get(repository_id)
        if baseline is None:
            if repository.created_at < baseline_at:
                continue
            baseline_stars = 0
        else:
            baseline_stars = baseline.stars
        star_delta = repository.stars - baseline_stars
        if star_delta <= 0:
            continue
        candidates.append(
            GitHubTrendCandidate(
                repository=repository,
                star_delta=star_delta,
                growth_ratio=star_delta / max(baseline_stars, 1),
            )
        )
    momentum = sorted(
        candidates,
        key=lambda candidate: (
            -candidate.star_delta,
            -candidate.repository.stars,
            candidate.repository.full_name.casefold(),
        ),
    )
    emerging_cutoff = run_at - EMERGING_MAX_AGE
    emerging = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.repository.created_at >= emerging_cutoff
        ),
        key=lambda candidate: (
            -(
                candidate.star_delta
                + candidate.growth_ratio * EMERGING_RATIO_WEIGHT
            ),
            -candidate.star_delta,
            candidate.repository.full_name.casefold(),
        ),
    )
    return momentum, emerging


def _recall_candidates(
    momentum: list[GitHubTrendCandidate],
    emerging: list[GitHubTrendCandidate],
) -> list[GitHubTrendCandidate]:
    recalled: list[GitHubTrendCandidate] = []
    seen: set[int] = set()
    for candidate in (
        momentum[:MOMENTUM_RECALL_LIMIT]
        + emerging[:EMERGING_RECALL_LIMIT]
    ):
        repository_id = candidate.repository.repository_id
        if repository_id in seen:
            continue
        seen.add(repository_id)
        recalled.append(candidate)
    return recalled


def _report_items(
    momentum: list[GitHubTrendCandidate],
    emerging: list[GitHubTrendCandidate],
    recommendations: dict[int, GitHubTrendRecommendation],
) -> list[GitHubTrendReportItem]:
    selected_ids: set[int] = set()
    selected_emerging: list[GitHubTrendCandidate] = []
    for candidate in emerging:
        repository_id = candidate.repository.repository_id
        if repository_id not in recommendations:
            continue
        selected_ids.add(repository_id)
        selected_emerging.append(candidate)
        if len(selected_emerging) == EMERGING_REPORT_LIMIT:
            break

    selected_momentum: list[GitHubTrendCandidate] = []
    for candidate in momentum:
        repository_id = candidate.repository.repository_id
        if repository_id not in recommendations or repository_id in selected_ids:
            continue
        selected_ids.add(repository_id)
        selected_momentum.append(candidate)
        if len(selected_momentum) == MOMENTUM_REPORT_LIMIT:
            break

    items: list[GitHubTrendReportItem] = []
    for category, candidates in (
        ("增长动量项目", selected_momentum),
        ("新兴项目", selected_emerging),
    ):
        for candidate in candidates:
            repository_id = candidate.repository.repository_id
            recommendation = recommendations[repository_id]
            items.append(
                GitHubTrendReportItem(
                    category=category,
                    repository=candidate.repository,
                    purpose=recommendation.purpose,
                    reason=recommendation.reason,
                    star_delta=candidate.star_delta,
                )
            )
    return items


async def _prepare_trend_report(
    context: GitHubTrendsContext,
    settings: GitHubTrendsSettings,
) -> GitHubTrendsPreparation:
    token = (
        None
        if settings.github_token is None
        else settings.github_token.get_secret_value()
    )
    repositories = await GitHubRepositorySearchClient(
        context.http_client, token
    ).snapshot(context.run_at)
    by_id = {
        str(repository.repository_id): repository
        for repository in repositories
    }
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
        period_start = (
            context.state.last_successful_report_at
            or context.state.baseline_at
        )
        if period_start is None:
            raise ValueError("GitHub trend baseline is invalid")
        if context.run_at - period_start < REPORT_INTERVAL:
            next_state = context.state.model_copy(update={"latest": snapshot})
            status = RunStatus.SNAPSHOT_UPDATED
        else:
            momentum, emerging = _trend_candidates(
                context.state, snapshot, context.run_at
            )
            recalled = _recall_candidates(momentum, emerging)
            if not recalled:
                return GitHubTrendsPreparation(
                    next_state=context.state.model_copy(update={"latest": snapshot}),
                    report=None,
                    repository_count=len(repositories),
                    no_report_status=RunStatus.EMPTY,
                )
            api_key, model = _report_credentials(settings)
            recommendations = await GitHubTrendAnalyzer(
                context.http_client,
                api_key=api_key,
                base_url=settings.ai_base_url,
                model=model,
            ).analyze(recalled)
            items = _report_items(momentum, emerging, recommendations)
            if not items:
                return GitHubTrendsPreparation(
                    next_state=context.state.model_copy(update={"latest": snapshot}),
                    report=None,
                    repository_count=len(repositories),
                    no_report_status=RunStatus.EMPTY,
                )
            next_state = GitHubTrendsState(
                baseline=snapshot,
                latest=snapshot,
                last_successful_report_at=context.run_at,
            )
            report_date = context.run_at.astimezone(
                ZoneInfo(settings.timezone)
            ).date().isoformat()
            parts = render_github_trends_report(
                items,
                period_start=period_start,
                period_end=context.run_at,
                report_date=report_date,
            )
            return GitHubTrendsPreparation(
                next_state=next_state,
                report=GitHubTrendsReport(
                    title=f"GitHub AI 趋势报告｜{report_date}",
                    parts=parts,
                    repository_count=len(items),
                ),
                repository_count=len(repositories),
            )
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
    async def prepare(context: GitHubTrendsContext) -> GitHubTrendsPreparation:
        return await _prepare_trend_report(context, settings)

    effective_sender_factory = sender_factory
    if effective_sender_factory is _sender_is_not_available:
        effective_sender_factory = lambda client: DingTalkSender(client, settings)

    return GitHubTrendsApplication(
        prepare,
        GitHubTrendsRuntime(
            clock=clock,
            http_client_factory=http_client_factory,
            state_store=GitHubTrendsStateFileStore(settings.state_path),
            sender_factory=effective_sender_factory,
            dry_run=settings.dry_run,
        ),
    )


async def run_github_trends(
    settings: GitHubTrendsSettings,
) -> GitHubTrendsRunResult:
    return await create_github_trends_application(settings).run()
