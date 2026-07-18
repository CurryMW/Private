import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

from ai_daily.analyzer import AnalysisError
from ai_daily.app import RunResult, run_digest
from ai_daily.config import load_settings, load_source_config
from ai_daily.dingtalk import DingTalkError


logger = logging.getLogger(__name__)


def _safe_error_message(error: Exception) -> str:
    if isinstance(error, (AnalysisError, DingTalkError)):
        return str(error)
    if isinstance(error, ValueError):
        return "configuration is invalid"
    if isinstance(error, OSError):
        return "required file operation failed"
    return "unexpected application error"


def _print_summary(result: RunResult) -> None:
    print(
        f"status={result.status} candidates={result.candidate_count} "
        f"selected={result.selected_count} parts={result.part_count}"
    )


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("ai_daily").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        load_dotenv()
        settings = load_settings()
        source_config = load_source_config(Path("config/sources.yaml"))
        result = asyncio.run(run_digest(settings, source_config))
    except Exception as error:
        logger.error(
            "run failed: %s: %s",
            type(error).__name__,
            _safe_error_message(error),
        )
        return 1

    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
