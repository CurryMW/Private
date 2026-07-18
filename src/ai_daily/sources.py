import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from html.parser import HTMLParser
from typing import Any

import feedparser
import httpx
from huggingface_hub import HfApi
from pydantic import ValidationError

from ai_daily.config import (
    ArxivConfig,
    HuggingFaceConfig,
    RssSource,
    SourceConfig,
)
from ai_daily.filtering import candidate_id
from ai_daily.models import Candidate


logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 20.0
_RETRY_DELAYS = (1, 2)
_ARXIV_API_URL = "https://export.arxiv.org/api/query"
_GITHUB_API_VERSION = "2026-03-10"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _clean_text(value: object, *, limit: int | None = None) -> str:
    parser = _TextExtractor()
    parser.feed(str(value or ""))
    parser.close()
    cleaned = " ".join("".join(parser.parts).split())
    return cleaned if limit is None else cleaned[:limit]


def _candidate(
    *,
    title: object,
    summary: object,
    source: str,
    url: str,
    published_at: datetime,
    source_kind: str,
) -> Candidate:
    return Candidate(
        id=candidate_id(url),
        title=_clean_text(title),
        summary=_clean_text(summary, limit=6000),
        source=source,
        url=url,
        published_at=_as_utc(published_at),
        source_kind=source_kind,
    )


def _as_utc(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _entry_timestamp(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return datetime(*parsed[:6], tzinfo=UTC)


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    for attempt in range(3):
        try:
            response = await client.get(
                url, timeout=_HTTP_TIMEOUT_SECONDS, **kwargs
            )
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            response.raise_for_status()
            return response
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError) as error:
            transient_status = (
                isinstance(error, httpx.HTTPStatusError)
                and (error.response.status_code == 429 or error.response.status_code >= 500)
            )
            if isinstance(error, httpx.HTTPStatusError) and not transient_status:
                raise
            if attempt == 2:
                raise
            await asyncio.sleep(_RETRY_DELAYS[attempt])
    raise RuntimeError("unreachable")


def _parse_feed_candidates(
    content: bytes,
    build_candidate: Callable[[Any, datetime], Candidate],
    cutoff: datetime,
) -> list[Candidate]:
    feed = feedparser.parse(content)
    parsed_candidates: list[Candidate] = []
    for entry in feed.entries:
        published_at = _entry_timestamp(entry)
        if published_at is None:
            continue
        try:
            parsed_candidates.append(build_candidate(entry, published_at))
        except (KeyError, TypeError, ValueError, ValidationError):
            continue

    if feed.bozo and not parsed_candidates:
        raise ValueError("feed contains no usable entries")
    return [candidate for candidate in parsed_candidates if candidate.published_at >= cutoff]


async def fetch_rss(
    source: RssSource,
    client: httpx.AsyncClient,
    cutoff: datetime,
) -> list[Candidate]:
    response = await _get_with_retry(client, str(source.url))

    def build(entry: Any, published_at: datetime) -> Candidate:
        return _candidate(
            title=entry["title"],
            summary=entry.get("summary", entry.get("description", "")),
            source=source.name,
            url=entry["link"],
            published_at=published_at,
            source_kind="rss",
        )

    return _parse_feed_candidates(response.content, build, cutoff)


async def fetch_arxiv(
    config: ArxivConfig,
    client: httpx.AsyncClient,
    cutoff: datetime,
) -> list[Candidate]:
    response = await _get_with_retry(
        client,
        _ARXIV_API_URL,
        params={
            "search_query": " OR ".join(
                f"cat:{category}" for category in config.categories
            ),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": config.max_results,
        },
    )

    def build(entry: Any, published_at: datetime) -> Candidate:
        arxiv_id = str(entry["id"]).rstrip("/").rsplit("/", 1)[-1]
        url = f"https://arxiv.org/abs/{arxiv_id}"
        return _candidate(
            title=entry["title"],
            summary=entry.get("summary", ""),
            source="arXiv",
            url=url,
            published_at=published_at,
            source_kind="arxiv",
        )

    return _parse_feed_candidates(response.content, build, cutoff)


async def fetch_github_releases(
    repository: str,
    client: httpx.AsyncClient,
    cutoff: datetime,
    github_token: str | None = None,
) -> list[Candidate]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    response = await _get_with_retry(
        client,
        f"https://api.github.com/repos/{repository}/releases",
        params={"per_page": 10},
        headers=headers,
    )

    candidates: list[Candidate] = []
    for release in response.json():
        if release.get("draft") or release.get("prerelease"):
            continue
        published_value = release.get("published_at")
        if not published_value:
            continue
        published_at = _as_utc(
            datetime.fromisoformat(str(published_value).replace("Z", "+00:00"))
        )
        if published_at < cutoff:
            continue
        candidates.append(
            _candidate(
                title=release.get("name") or release["tag_name"],
                summary=release.get("body", ""),
                source=f"GitHub: {repository}",
                url=release["html_url"],
                published_at=published_at,
                source_kind="github",
            )
        )
    return candidates


def _dates_in_window(cutoff: datetime, now: datetime) -> list[date]:
    current = _as_utc(cutoff).date()
    final = _as_utc(now).date()
    dates: list[date] = []
    while current <= final:
        dates.append(current)
        current += timedelta(days=1)
    return dates


async def fetch_huggingface_papers(
    config: HuggingFaceConfig,
    cutoff: datetime,
    now: datetime,
) -> list[Candidate]:
    if not config.enabled:
        return []

    api = HfApi()
    candidates: list[Candidate] = []
    for requested_date in _dates_in_window(cutoff, now):
        date_string = requested_date.isoformat()

        def list_papers() -> list[Any]:
            return list(
                api.list_daily_papers(
                    date=date_string,
                    sort="trending",
                    limit=config.limit_per_day,
                    token=False,
                )
            )

        papers = await asyncio.to_thread(list_papers)
        fallback_timestamp = datetime.combine(requested_date, time(12), tzinfo=UTC)
        for paper in papers:
            exact_timestamp = (
                getattr(paper, "submitted_at", None)
                or getattr(paper, "published_at", None)
            )
            published_at = _as_utc(exact_timestamp or fallback_timestamp)
            if published_at < cutoff or (
                exact_timestamp is not None and published_at > now
            ):
                continue
            paper_id = str(paper.id)
            candidates.append(
                _candidate(
                    title=getattr(paper, "title", None) or paper_id,
                    summary=(
                        getattr(paper, "summary", None)
                        or getattr(paper, "ai_summary", None)
                        or ""
                    ),
                    source="Hugging Face Papers",
                    url=f"https://huggingface.co/papers/{paper_id}",
                    published_at=published_at,
                    source_kind="huggingface",
                )
            )
    return candidates


async def collect_candidates(
    config: SourceConfig,
    client: httpx.AsyncClient,
    cutoff: datetime,
    now: datetime,
    github_token: str | None,
) -> list[Candidate]:
    semaphore = asyncio.Semaphore(8)

    async def bounded(
        operation: Callable[[], Awaitable[list[Candidate]]],
    ) -> list[Candidate]:
        async with semaphore:
            return await operation()

    operations: list[tuple[str, Callable[[], Awaitable[list[Candidate]]]]] = []
    for source in config.rss:
        operations.append(
            (
                source.name,
                lambda source=source: fetch_rss(source, client, cutoff),
            )
        )
    if config.arxiv is not None:
        operations.append(
            (
                "arXiv",
                lambda: fetch_arxiv(config.arxiv, client, cutoff),
            )
        )
    if (
        config.huggingface_daily_papers is not None
        and config.huggingface_daily_papers.enabled
    ):
        operations.append(
            (
                "Hugging Face Papers",
                lambda: fetch_huggingface_papers(
                    config.huggingface_daily_papers, cutoff, now
                ),
            )
        )
    for repository in config.github_repositories:
        operations.append(
            (
                f"GitHub: {repository}",
                lambda repository=repository: fetch_github_releases(
                    repository, client, cutoff, github_token
                ),
            )
        )

    results = await asyncio.gather(
        *(bounded(operation) for _, operation in operations),
        return_exceptions=True,
    )
    candidates: list[Candidate] = []
    for (label, _), result in zip(operations, results, strict=True):
        if isinstance(result, BaseException):
            logger.error("source failed: %s: %s", label, type(result).__name__)
            continue
        candidates.extend(result)
    return candidates
