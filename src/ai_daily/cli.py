import asyncio
import logging
import re
from pathlib import Path

from dotenv import load_dotenv

from ai_daily.analyzer import AnalysisError
from ai_daily.app import RunResult, run_digest
from ai_daily.baidu_search import BaiduSearchError
from ai_daily.config import load_settings, load_source_config
from ai_daily.dingtalk import DingTalkError


logger = logging.getLogger(__name__)


_SAFE_VALUE_ERROR_PATTERNS = (
    re.compile(r"AI_API_KEY is required"),
    re.compile(r"AI_MODEL must be gpt-5\.6-luna"),
    re.compile(r"AI_BASE_URL must be an HTTPS URL"),
    re.compile(r"DINGTALK_WEBHOOK must be an HTTPS URL"),
    re.compile(r"DINGTALK_ACCESS_TOKEN is required for a base webhook"),
    re.compile(r"webhook must contain exactly one nonblank access_token"),
    re.compile(r"a complete search plan requires .+"),
)


def _safe_error_message(error: Exception) -> str:
    if isinstance(error, (AnalysisError, BaiduSearchError, DingTalkError)):
        return str(error)
    if isinstance(error, ValueError):
        message = str(error)
        for pattern in _SAFE_VALUE_ERROR_PATTERNS:
            match = pattern.search(message)
            if match is not None:
                return match.group(0)
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
