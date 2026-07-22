import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import httpx

from ai_daily.analyzer import AnalysisError, Analyzer
from ai_daily.config import Settings, SourceConfig
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
    status: Literal["sent", "dry-run", "empty", "already-sent"]
    candidate_count: int
    selected_count: int
    part_count: int


async def run_digest(
    settings: Settings,
    source_config: SourceConfig,
    now: datetime | None = None,
) -> RunResult:
    run_at = datetime.now(UTC) if now is None else now
    if run_at.tzinfo is None or run_at.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    run_at = run_at.astimezone(UTC)
    report_date = run_at.astimezone(ZoneInfo(settings.timezone)).date()
    delivery_state = DeliveryState()
    if settings.enforce_daily_once:
        delivery_state = DeliveryState.load(settings.delivery_state_path)
        if delivery_state.is_delivered(report_date):
            logger.info("status=already-sent")
            return RunResult(
                status="already-sent",
                candidate_count=0,
                selected_count=0,
                part_count=0,
            )

    collection_cutoff = run_at - timedelta(hours=settings.fallback_window_hours)
    sent_state = SentState.load(settings.state_path)
    github_token = (
        None
        if settings.github_token is None
        else settings.github_token.get_secret_value()
    )

    async with httpx.AsyncClient(
        headers={"User-Agent": "dingtalk-ai-daily/0.1"}
    ) as client:
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
            for index, part in enumerate(parts, 1):
                print(f"--- preview {index}/{len(parts)} ---")
                print(part)
            logger.info("status=dry-run")
            return RunResult(
                status="dry-run",
                candidate_count=len(batch.candidates),
                selected_count=selected_count,
                part_count=len(parts),
            )

        title = f"{report_title}｜{report_date.isoformat()}"
        await DingTalkSender(client, settings).send(parts, title)

    if digest is not None:
        sent_state.mark_sent((str(item.url) for item in digest.items), run_at)
        sent_state.save(settings.state_path)
    if settings.enforce_daily_once:
        delivery_state.mark_delivered(report_date, run_at)
        delivery_state.save(settings.delivery_state_path)
    logger.info("status=sent")
    return RunResult(
        status="sent",
        candidate_count=len(batch.candidates),
        selected_count=selected_count,
        part_count=len(parts),
    )
