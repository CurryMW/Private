from datetime import UTC, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import ValidationError

from ai_daily.filtering import candidate_id, canonicalize_url
from ai_daily.models import SearchLead


BAIDU_WEB_SEARCH_ENDPOINT = (
    "https://qianfan.baidubce.com/v2/ai_search/web_search"
)


class BaiduSearchError(RuntimeError):
    """A safe search error that never includes credentials or response bodies."""


class BaiduSearchClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        *,
        timezone: str,
    ) -> None:
        if not api_key.strip():
            raise BaiduSearchError("BAIDU_SEARCH_API_KEY is required")
        try:
            self._timezone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            raise BaiduSearchError("configured timezone is invalid") from None
        self._client = client
        self._api_key = api_key

    async def search(self, query: str) -> list[SearchLead]:
        try:
            response = await self._client.post(
                BAIDU_WEB_SEARCH_ENDPOINT,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "messages": [{"role": "user", "content": query}],
                    "search_source": "baidu_search_v2",
                    "resource_type_filter": [{"type": "web", "top_k": 20}],
                },
                timeout=20.0,
            )
        except httpx.RequestError:
            raise BaiduSearchError("Baidu search request failed") from None

        if not response.is_success:
            raise BaiduSearchError(
                f"Baidu search request failed with HTTP {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError:
            raise BaiduSearchError("Baidu search response is invalid") from None
        if not isinstance(payload, dict) or payload.get("code") is not None:
            raise BaiduSearchError("Baidu search returned an error")
        references = payload.get("references")
        if not isinstance(references, list):
            raise BaiduSearchError("Baidu search response is invalid")

        leads: list[SearchLead] = []
        for reference in references:
            lead = self._parse_reference(reference)
            if lead is not None:
                leads.append(lead)
        return leads

    def _parse_reference(self, reference: object) -> SearchLead | None:
        if not isinstance(reference, dict) or reference.get("type") != "web":
            return None
        try:
            raw_url = str(reference["url"])
            split_url = urlsplit(raw_url)
            if split_url.username is not None or split_url.password is not None:
                return None
            url = canonicalize_url(raw_url)
            published_at = _published_at(reference.get("date"), self._timezone)
            if published_at is None:
                return None
            title = _plain_text(reference.get("title"), limit=300)
            snippet = _plain_text(
                reference.get("snippet") or reference.get("content"),
                limit=6000,
            )
            source = _plain_text(
                reference.get("website")
                or reference.get("web_anchor")
                or split_url.hostname,
                limit=100,
            )
            return SearchLead(
                id=candidate_id(url),
                title=title,
                snippet=snippet,
                source=source,
                url=url,
                published_at=published_at,
                relevance_score=reference.get("rerank_score"),
                authority_score=reference.get("authority_score"),
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            return None


def _plain_text(value: object, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _published_at(value: object, timezone: ZoneInfo) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        timestamp = timestamp.replace(tzinfo=timezone)
    return timestamp.astimezone(UTC)
