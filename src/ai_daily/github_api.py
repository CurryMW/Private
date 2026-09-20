from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
)

from ai_daily.github_trends_state import (
    GitHubRepositorySnapshot,
    require_aware_timestamp,
)


GITHUB_REPOSITORY_SEARCH_ENDPOINT = "https://api.github.com/search/repositories"
GITHUB_API_VERSION = "2026-03-10"
REPOSITORY_CANDIDATE_LIMIT = 200
SEARCH_PAGE_SIZE = 100
MAXIMUM_SEARCH_PAGES = REPOSITORY_CANDIDATE_LIMIT // SEARCH_PAGE_SIZE
RECENT_ACTIVITY_DAYS = 180
MINIMUM_DESCRIPTION_LENGTH = 24
MINIMUM_README_BYTES = 400
README_CHECK_CONCURRENCY = 8
MAX_ATTEMPTS = 3
AI_REPOSITORY_SEARCH_QUERY = (
    "ai OR llm OR agent OR inference OR machine-learning "
    "in:name,description,topics"
)
AI_REPOSITORY_PATTERNS = tuple(
    re.compile(pattern, flags=re.IGNORECASE)
    for pattern in (
        r"\b(?:ai|ml|llm|mlops|cuda|gpu)\b",
        r"\bartificial[- ]intelligence\b",
        r"\bmachine[- ]learning\b",
        r"\bdeep[- ]learning\b",
        r"\blarge[- ]language[- ]models?\b",
        r"\b(?:ai|llm|autonomous|multi)[- ]agents?\b",
        r"\bagentic\b",
        r"\binference\b",
        r"\bmodel[- ]serving\b",
        r"\bembeddings?\b",
        r"\bvector[- ]databases?\b",
        r"\btransformers?\b",
        r"人工智能|机器学习|大模型|智能体|模型推理",
    )
)


class GitHubAPIError(RuntimeError):
    """Safe GitHub API failure without response bodies or credentials."""


class _RepositoryPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(gt=0)
    full_name: str = Field(
        min_length=3,
        max_length=200,
        pattern=r"^[^/\s]+/[^/\s]+$",
    )
    html_url: HttpUrl
    description: str | None = None
    fork: bool
    archived: bool
    stargazers_count: int = Field(ge=0)
    language: str | None = None
    created_at: datetime
    pushed_at: datetime
    topics: tuple[str, ...] = ()

    _timestamps_are_aware = field_validator("created_at", "pushed_at")(
        require_aware_timestamp
    )


class _ReadmePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["file"]
    size: int = Field(ge=0)
    html_url: HttpUrl


class GitHubRepositorySearchClient:
    def __init__(self, client: httpx.AsyncClient, token: str | None) -> None:
        self._client = client
        self._token = token.strip() if token is not None else ""

    async def snapshot(self, sampled_at: datetime) -> list[GitHubRepositorySnapshot]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        repositories: list[_RepositoryPayload] = []
        seen_repository_ids: set[int] = set()
        target_count: int | None = None
        for page in range(1, MAXIMUM_SEARCH_PAGES + 1):
            total_count, page_repositories = await self._fetch_page(page, headers)
            if target_count is None:
                target_count = min(total_count, REPOSITORY_CANDIDATE_LIMIT)

            assert target_count is not None
            expected_page_count = min(
                SEARCH_PAGE_SIZE,
                max(total_count - ((page - 1) * SEARCH_PAGE_SIZE), 0),
            )
            if len(page_repositories) != expected_page_count:
                raise GitHubAPIError("GitHub search response is incomplete")

            for repository in page_repositories:
                if repository.id in seen_repository_ids:
                    continue
                seen_repository_ids.add(repository.id)
                repositories.append(repository)
                if len(repositories) == target_count:
                    break
            if len(repositories) == target_count:
                break
        if target_count is None or not repositories:
            raise GitHubAPIError("GitHub search response is incomplete")

        metadata_eligible = [
            repository
            for repository in repositories
            if self._is_eligible(repository, sampled_at)
        ]
        readmes = await self._fetch_readmes(metadata_eligible, headers)
        snapshots = [
            self._as_snapshot(repository, readme)
            for repository, readme in zip(
                metadata_eligible, readmes, strict=True
            )
            if readme is not None and readme.size >= MINIMUM_README_BYTES
        ]
        if not snapshots:
            raise GitHubAPIError("GitHub snapshot has no eligible repositories")
        return snapshots

    async def _fetch_page(
        self,
        page: int,
        headers: dict[str, str],
    ) -> tuple[int, list[_RepositoryPayload]]:
        response = await self._get_with_retry(
            GITHUB_REPOSITORY_SEARCH_ENDPOINT,
            params={
                "q": AI_REPOSITORY_SEARCH_QUERY,
                "sort": "updated",
                "order": "desc",
                "per_page": SEARCH_PAGE_SIZE,
                "page": page,
            },
            headers=headers,
            error_label="search",
        )
        if not response.is_success:
            raise GitHubAPIError(
                f"GitHub search request failed with HTTP {response.status_code}"
            )
        try:
            payload = response.json()
            if (
                not isinstance(payload, dict)
                or payload.get("incomplete_results") is not False
                or type(payload.get("total_count")) is not int
                or payload["total_count"] < 0
                or not isinstance(payload.get("items"), list)
            ):
                raise TypeError
            return (
                payload["total_count"],
                [
                    _RepositoryPayload.model_validate(item)
                    for item in payload["items"]
                ],
            )
        except (TypeError, ValueError, ValidationError):
            raise GitHubAPIError("GitHub search response is invalid") from None

    async def _fetch_readmes(
        self,
        repositories: list[_RepositoryPayload],
        headers: dict[str, str],
    ) -> list[_ReadmePayload | None]:
        semaphore = asyncio.Semaphore(README_CHECK_CONCURRENCY)

        async def bounded(repository: _RepositoryPayload) -> _ReadmePayload | None:
            async with semaphore:
                return await self._fetch_readme(repository, headers)

        return list(await asyncio.gather(*(bounded(repo) for repo in repositories)))

    async def _fetch_readme(
        self,
        repository: _RepositoryPayload,
        headers: dict[str, str],
    ) -> _ReadmePayload | None:
        repository_path = quote(repository.full_name, safe="/")
        response = await self._get_with_retry(
            f"https://api.github.com/repos/{repository_path}/readme",
            headers=headers,
            error_label="README",
        )
        if response.status_code == 404:
            return None
        if not response.is_success:
            raise GitHubAPIError(
                f"GitHub README request failed with HTTP {response.status_code}"
            )
        try:
            return _ReadmePayload.model_validate(response.json())
        except (ValueError, ValidationError):
            raise GitHubAPIError("GitHub README response is invalid") from None

    async def _get_with_retry(
        self,
        url: str,
        *,
        headers: dict[str, str],
        error_label: str,
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self._client.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=20.0,
                )
            except (httpx.ConnectError, httpx.TimeoutException):
                if attempt == MAX_ATTEMPTS - 1:
                    raise GitHubAPIError(
                        f"GitHub {error_label} request failed"
                    ) from None
                await asyncio.sleep(attempt + 1)
                continue
            except httpx.RequestError:
                raise GitHubAPIError(
                    f"GitHub {error_label} request failed"
                ) from None

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(attempt + 1)
                    continue
            return response

        raise AssertionError("GitHub request retry loop exhausted")

    @staticmethod
    def _is_eligible(repository: _RepositoryPayload, sampled_at: datetime) -> bool:
        description = " ".join((repository.description or "").split())
        if repository.fork or repository.archived:
            return False
        if len(description) < MINIMUM_DESCRIPTION_LENGTH:
            return False
        if repository.pushed_at.astimezone(UTC) < sampled_at - timedelta(
            days=RECENT_ACTIVITY_DAYS
        ):
            return False
        searchable = " ".join(
            (repository.full_name, description, *repository.topics)
        ).casefold()
        return any(pattern.search(searchable) for pattern in AI_REPOSITORY_PATTERNS)

    @staticmethod
    def _as_snapshot(
        repository: _RepositoryPayload,
        readme: _ReadmePayload,
    ) -> GitHubRepositorySnapshot:
        return GitHubRepositorySnapshot(
            repository_id=repository.id,
            full_name=repository.full_name,
            url=repository.html_url,
            description=" ".join((repository.description or "").split()),
            created_at=repository.created_at.astimezone(UTC),
            stars=repository.stargazers_count,
            language=repository.language,
            pushed_at=repository.pushed_at.astimezone(UTC),
            topics=repository.topics,
            readme_url=readme.html_url,
            readme_size=readme.size,
        )
