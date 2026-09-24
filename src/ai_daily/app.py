import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx

from ai_daily.analyzer import Analyzer
from ai_daily.baidu_search import BaiduSearchClient, BaiduSearchError
from ai_daily.baidu_usage import BaiduSearchUsage, BaiduSearchUsageFileStore
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
)
from ai_daily.evidence import prepare_search_candidates
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


def _window_scope(window_hours: int) -> str:
    if window_hours % 24 == 0:
        return f"{window_hours // 24} 天"
    return f"{window_hours} 小时"


@dataclass(frozen=True)
class AIDigestRuntime:
    clock: Callable[[], datetime]
    http_client_factory: HTTPClientFactory
    sent_state_store: StateStore[SentState]
    delivery_state_store: StateStore[DeliveryState]
    sender_factory: SenderFactory
    baidu_usage_store: StateStore[BaiduSearchUsage] | None = None


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
        baidu_usage_store=BaiduSearchUsageFileStore(settings.baidu_usage_state_path),
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

        return await self._run_baidu_digest(
            run_at,
            report_date,
            source_config.baidu_search,
            delivery_state,
        )

    async def _run_baidu_digest(
        self,
        run_at: datetime,
        report_date: date,
        search_config: BaiduSearchConfig,
        delivery_state: DeliveryState,
    ) -> RunResult:
        settings = self._settings
        search_key = settings.baidu_search_api_key
        if search_key is None or not search_key.get_secret_value().strip():
            raise BaiduSearchError("BAIDU_SEARCH_API_KEY is required")
        sent_state = self._runtime.sent_state_store.load()
        usage = (
            self._runtime.baidu_usage_store.load()
            if self._runtime.baidu_usage_store is not None
            else BaiduSearchUsage()
        )

        async with self._runtime.http_client_factory() as client:
            search = BaiduSearchClient(
                client,
                search_key.get_secret_value(),
                timezone=settings.timezone,
            )
            leads = []
            for query in search_config.queries_for(report_date.toordinal()):
                usage.reserve(report_date)
                if self._runtime.baidu_usage_store is not None:
                    self._runtime.baidu_usage_store.save(usage)
                leads.extend(await search.search(query))
            logger.info("search_leads=%d", len(leads))
            candidates = prepare_search_candidates(
                leads,
                now=run_at,
                window_hours=settings.window_hours,
                event_dedupe_days=settings.event_dedupe_days,
                sent_state=sent_state,
                search_config=search_config,
            )
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
                scope_text=(
                    "覆盖范围：百度可检索到的中英文 AI 公开信息"
                    f"（最近 {_window_scope(settings.window_hours)}）"
                ),
                evidence_candidates=candidates,
                evidence_timezone=settings.timezone,
            )
            if settings.dry_run:
                return self._preview_result(
                    parts,
                    candidate_count=len(candidates),
                    selected_count=len(digest.items),
                )

            title = f"AI 情报摘要｜{report_date.isoformat()}"
            await self._runtime.sender_factory(client, settings).send(parts, title)

        selected_urls = [str(item.url) for item in digest.items]
        sent_state.mark_sent(selected_urls, run_at)
        candidates_by_url = {
            str(candidate.url): candidate for candidate in candidates
        }
        for selected_url in selected_urls:
            candidate = candidates_by_url[selected_url]
            sent_state.record_event(
                candidate.title,
                candidate.summary,
                run_at,
                event_dedupe_days=settings.event_dedupe_days,
            )
        self._runtime.sent_state_store.save(sent_state)
        if settings.enforce_daily_once:
            delivery_state.mark_delivered(report_date, run_at)
            self._runtime.delivery_state_store.save(delivery_state)
        logger.info("status=sent")
        return RunResult(
            status=RunStatus.SENT,
            candidate_count=len(candidates),
            selected_count=len(digest.items),
            part_count=len(parts),
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
