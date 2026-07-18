import asyncio
import json

import httpx
from pydantic import ValidationError

from ai_daily.config import Settings
from ai_daily.filtering import canonicalize_url
from ai_daily.models import Candidate, Digest


SYSTEM_PROMPT = """你是严谨的 AI 技术编辑。只能使用候选材料中的事实，不得编造数字、日期、能力、评测结果或链接。
只选择技术、模型、研究、开源工具与 AI 范式内容，排除融资、股价、人事和营销新闻。
最多选择 8 条；质量不足时可以少选。趋势判断必须与事实摘要分开。
仅返回一个 JSON 对象，不要使用 Markdown 代码块。"""


class AnalysisError(RuntimeError):
    """Raised when model analysis cannot produce a safe, valid digest."""


class Analyzer:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def analyze(self, candidates: list[Candidate]) -> Digest:
        response = await self._request(candidates)
        content = _response_content(response)
        try:
            digest = Digest.model_validate_json(_strip_json_fence(content))
        except (ValidationError, ValueError, TypeError) as error:
            raise AnalysisError("analysis validation failed") from None

        if len(digest.items) > self._settings.max_items:
            raise AnalysisError("analysis exceeds configured maximum items")

        evidence_urls = {
            canonicalize_url(str(candidate.url)) for candidate in candidates
        }
        if any(
            canonicalize_url(str(item.url)) not in evidence_urls
            for item in digest.items
        ):
            raise AnalysisError("analysis evidence URL is not a candidate")
        return digest

    async def _request(self, candidates: list[Candidate]) -> httpx.Response:
        endpoint = f"{self._settings.ai_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self._settings.ai_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_message(candidates)},
            ],
            "temperature": 0.2,
            "max_tokens": 3000,
        }
        headers = {
            "Authorization": (
                f"Bearer {self._settings.ai_api_key.get_secret_value()}"
            )
        }

        for attempt in range(3):
            try:
                response = await self._client.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=180,
                )
            except httpx.TimeoutException:
                if attempt == 2:
                    raise AnalysisError(
                        "AI analysis timed out after 3 attempts"
                    ) from None
                await asyncio.sleep(attempt + 1)
                continue
            except httpx.ConnectError:
                if attempt == 2:
                    raise AnalysisError(
                        "AI analysis connection failed after 3 attempts"
                    ) from None
                await asyncio.sleep(attempt + 1)
                continue

            if response.status_code == 429:
                if attempt == 2:
                    raise AnalysisError(
                        "AI analysis rate limited after 3 attempts"
                    )
                await asyncio.sleep(attempt + 1)
                continue
            if 500 <= response.status_code < 600:
                if attempt == 2:
                    raise AnalysisError(
                        "AI analysis service failed with "
                        f"HTTP {response.status_code} after 3 attempts"
                    )
                await asyncio.sleep(attempt + 1)
                continue
            if not response.is_success:
                raise AnalysisError(
                    f"AI analysis request failed with HTTP {response.status_code}"
                )
            return response

        raise AnalysisError("AI analysis request failed")


def _user_message(candidates: list[Candidate]) -> str:
    evidence = [
        {
            "id": candidate.id,
            "title": candidate.title,
            "summary": candidate.summary,
            "source": candidate.source,
            "url": str(candidate.url),
            "published_at": candidate.published_at.isoformat(),
        }
        for candidate in candidates
    ]
    return "\n".join(
        [
            "候选材料：",
            json.dumps(evidence, ensure_ascii=False),
            "输出 JSON Schema：",
            json.dumps(Digest.model_json_schema(), ensure_ascii=False),
        ]
    )


def _response_content(response: httpx.Response) -> str:
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise AnalysisError("AI response format is invalid") from None
    if not isinstance(content, str):
        raise AnalysisError("AI response format is invalid")
    return content


def _strip_json_fence(content: str) -> str:
    stripped = content.strip()
    lines = stripped.splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().casefold() in {"```", "```json"}
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip()
    return stripped
