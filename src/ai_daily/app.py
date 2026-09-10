import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from ai_daily.analyzer import AnalysisError, Analyzer
from ai_daily.baidu_search import BaiduSearchClient, BaiduSearchError
from ai_daily.application import (
    DeliveryStateFileStore,
    HTTPClientFactory,
    MessageSender,
    RunStatus,
    SentStateFileStore,
    StateStore,
)
from ai_daily.config import BaiduSearchConfig, Settings, SourceConfig
from ai_daily.delivery_state import DeliveryState
from ai_daily.dingtalk import (
    DingTalkSender,
    render_digest,
    render_model_service_notice,
    render_status_notice,
)
from ai_daily.selection import select_candidate_batch
from ai_daily.sources import collect_candidates
from ai_daily.state import SentState


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    candidate_count: int
    selected_count: int
    part_count: int
    failure_type: str | None = None
    _failure: Exception | None = field(default=None, compare=False, repr=False)

    @classmethod
    def failed(cls, error: Exception) -> "RunResult":
        return cls(
            status=RunStatus.FAILED,
            candidate_count=0,
            selected_count=0,
            part_count=0,
            failure_type=type(error).__name__,
            _failure=error,
        )


SenderFactory = Callable[[httpx.AsyncClient, Settings], MessageSender]


@dataclass(frozen=True)
class AIDigestRuntime:
    clock: Callable[[], datetime]
    http_client_factory: HTTPClientFactory
    sent_state_store: StateStore[SentState]
    delivery_state_store: StateStore[DeliveryState]
    sender_factory: SenderFactory


def _production_runtime(settings: Settings) -> AIDigestRuntime:
    def create_http_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"User-Agent": "dingtalk-ai-daily/0.1"}
        )

    return AIDigestRuntime(
        clock=lambda: datetime.now(UTC),
        http_client_factory=create_http_client,
        sent_state_store=SentStateFileStore(settings.state_path),
        delivery_state_store=DeliveryStateFileStore(
            settings.delivery_state_path
        ),
        sender_factory=lambda client, configured_settings: DingTalkSender(
            client, configured_settings
        ),
    )


class AIDigestApplication:
    def __init__(
        self,
        settings: Settings,
        source_config: SourceConfig,
        *,
        runtime: AIDigestRuntime | None = None,
    ) -> None:
        self._settings = settings
        self._source_config = source_config
        self._runtime = runtime or _production_runtime(settings)

    async def run(self, now: datetime | None = None) -> RunResult:
        try:
            return await self._run(now)
        except Exception as error:
            return RunResult.failed(error)

    async def _run(self, now: datetime | None) -> RunResult:
        settings = self._settings
        source_config = self._source_config
        runtime = self._runtime
        run_at = runtime.clock() if now is None else now
        if run_at.tzinfo is None or run_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        run_at = run_at.astimezone(UTC)
        report_date = run_at.astimezone(ZoneInfo(settings.timezone)).date()
        if source_config.baidu_search is not None:
            return await self._run_baidu_preview(
                report_date, source_config.baidu_search
            )

        delivery_state = DeliveryState()
        if settings.enforce_daily_once:
            delivery_state = runtime.delivery_state_store.load()
            if delivery_state.is_delivered(report_date):
                logger.info("status=already-sent")
                return RunResult(
                    status=RunStatus.ALREADY_PROCESSED,
                    candidate_count=0,
                    selected_count=0,
                    part_count=0,
                )

        collection_cutoff = run_at - timedelta(hours=settings.fallback_window_hours)
        sent_state = runtime.sent_state_store.load()
        github_token = (
            None
            if settings.github_token is None
            else settings.github_token.get_secret_value()
        )

        async with runtime.http_client_factory() as client:
            collected = await collect_candidates(
                source_config,
                client,
                collection_cutoff,
                run_at,
                github_token,
            )
            logger.info("collected=%d", len(collected))

            batch = select_candidate_batch(
                collected,
                now=run_at,
                sent_state=sent_state,
                primary_window_hours=settings.window_hours,
                fallback_window_hours=settings.fallback_window_hours,
                max_items=settings.max_items,
            )
            logger.info("prepared=%d", len(batch.candidates))
            logger.info("mode=%s", batch.mode)

            digest = None
            report_title = "AI 技术日报"
            if batch.mode == "notice":
                parts = render_status_notice(report_date)
                selected_count = 0
            else:
                intro = None
                scope_text = None
                if batch.mode == "extended":
                    report_title = "AI 近期技术精选"
                    scope_text = "信息范围：最近 7 天"
                elif batch.mode == "review":
                    report_title = "AI 近期技术回顾"
                    intro = "今日无新的合格动态，以下为近期值得回顾的技术内容。"
                    scope_text = "回顾范围：最近 7 天"

                model_candidates = batch.candidates[: settings.model_candidate_limit]
                analyzer = Analyzer(client, settings)
                try:
                    digest = await analyzer.analyze(
                        model_candidates,
                        max_items=batch.max_items,
                    )
                except AnalysisError as error:
                    retry_candidates = model_candidates[
                        : settings.model_retry_candidate_limit
                    ]
                    if (
                        error.retry_with_smaller_input
                        and len(retry_candidates) < len(model_candidates)
                    ):
                        logger.info(
                            "retrying analysis with candidates=%d",
                            len(retry_candidates),
                        )
                        try:
                            digest = await analyzer.analyze(
                                retry_candidates,
                                max_items=batch.max_items,
                            )
                        except AnalysisError:
                            digest = None
                    else:
                        digest = None

                if digest is None:
                    logger.warning("analysis failed; sending model service notice")
                    report_title = "AI 技术日报"
                    parts = render_model_service_notice(report_date)
                    selected_count = 0
                else:
                    parts = render_digest(
                        digest,
                        report_date,
                        batch.window_hours,
                        report_title=report_title,
                        intro=intro,
                        scope_text=scope_text,
                    )
                    selected_count = len(digest.items)
            logger.info("selected=%d", selected_count)
            logger.info("parts=%d", len(parts))

            if settings.dry_run:
                return self._preview_result(
                    parts,
                    candidate_count=len(batch.candidates),
                    selected_count=selected_count,
                )

            title = f"{report_title}｜{report_date.isoformat()}"
            await runtime.sender_factory(client, settings).send(parts, title)

        if digest is not None:
            sent_state.mark_sent((str(item.url) for item in digest.items), run_at)
            runtime.sent_state_store.save(sent_state)
        if settings.enforce_daily_once:
            delivery_state.mark_delivered(report_date, run_at)
            runtime.delivery_state_store.save(delivery_state)
        logger.info("status=sent")
        return RunResult(
            status=RunStatus.SENT,
            candidate_count=len(batch.candidates),
            selected_count=selected_count,
            part_count=len(parts),
        )

    async def _run_baidu_preview(
        self,
        report_date: date,
        search_config: BaiduSearchConfig,
    ) -> RunResult:
        settings = self._settings
        if not settings.dry_run:
            raise BaiduSearchError("Baidu search tracer requires dry-run mode")
        search_key = settings.baidu_search_api_key
        if search_key is None or not search_key.get_secret_value().strip():
            raise BaiduSearchError("BAIDU_SEARCH_API_KEY is required")

        async with self._runtime.http_client_factory() as client:
            search = BaiduSearchClient(
                client,
                search_key.get_secret_value(),
                timezone=settings.timezone,
            )
            leads = await search.search(search_config.query)
            candidates = [lead.as_candidate() for lead in leads]
            logger.info("collected=%d", len(candidates))
            if not candidates:
                logger.info("status=empty")
                return RunResult(
                    status=RunStatus.EMPTY,
                    candidate_count=0,
                    selected_count=0,
                    part_count=0,
                )

            digest = await Analyzer(client, settings).analyze(candidates)
            parts = render_digest(
                digest,
                report_date,
                settings.window_hours,
                report_title="AI 情报摘要",
                evidence_candidates=candidates,
                evidence_timezone=settings.timezone,
            )
            return self._preview_result(
                parts,
                candidate_count=len(candidates),
                selected_count=len(digest.items),
            )

    @staticmethod
    def _preview_result(
        parts: list[str],
        *,
        candidate_count: int,
        selected_count: int,
    ) -> RunResult:
        for index, part in enumerate(parts, 1):
            print(f"--- preview {index}/{len(parts)} ---")
            print(part)
        logger.info("status=dry-run")
        return RunResult(
            status=RunStatus.PREVIEW,
            candidate_count=candidate_count,
            selected_count=selected_count,
            part_count=len(parts),
        )


async def run_digest(
    settings: Settings,
    source_config: SourceConfig,
    now: datetime | None = None,
) -> RunResult:
    result = await AIDigestApplication(settings, source_config).run(now)
    if result.status is RunStatus.FAILED:
        if result._failure is None:
            raise RuntimeError("AI digest failed")
        raise result._failure
    return result
