import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import httpx

from ai_daily.analyzer import Analyzer
from ai_daily.config import Settings, SourceConfig
from ai_daily.dingtalk import DingTalkSender, render_digest
from ai_daily.filtering import prepare_candidates
from ai_daily.sources import collect_candidates
from ai_daily.state import SentState


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunResult:
    status: Literal["sent", "dry-run", "empty"]
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
    cutoff = run_at - timedelta(hours=settings.window_hours)
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
            cutoff,
            run_at,
            github_token,
        )
        logger.info("collected=%d", len(collected))

        candidates = prepare_candidates(collected, cutoff, sent_state)
        logger.info("prepared=%d", len(candidates))
        if not candidates:
            logger.info("selected=0")
            logger.info("parts=0")
            logger.info("status=empty")
            return RunResult(
                status="empty",
                candidate_count=0,
                selected_count=0,
                part_count=0,
            )

        digest = await Analyzer(client, settings).analyze(candidates)
        report_date = run_at.astimezone(ZoneInfo(settings.timezone)).date()
        parts = render_digest(digest, report_date, settings.window_hours)
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
                candidate_count=len(candidates),
                selected_count=selected_count,
                part_count=len(parts),
            )

        title = f"AI 技术日报｜{report_date.isoformat()}"
        await DingTalkSender(client, settings).send(parts, title)

    sent_state.mark_sent((str(item.url) for item in digest.items), run_at)
    sent_state.save(settings.state_path)
    logger.info("status=sent")
    return RunResult(
        status="sent",
        candidate_count=len(candidates),
        selected_count=selected_count,
        part_count=len(parts),
    )
