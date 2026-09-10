import asyncio
import logging

from dotenv import load_dotenv

from ai_daily.application import RunStatus
from ai_daily.github_trends import (
    load_github_trends_settings,
    run_github_trends,
)


logger = logging.getLogger(__name__)


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
        settings = load_github_trends_settings()
        result = asyncio.run(run_github_trends(settings))
    except Exception as error:
        logger.error("run failed: %s", type(error).__name__)
        return 1

    print(
        f"status={result.status} repositories={result.repository_count} "
        f"parts={result.part_count}"
    )
    if result.status is RunStatus.FAILED:
        logger.error("run failed: %s", result.failure_type or "UnknownError")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
