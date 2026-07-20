import asyncio
import re
from collections.abc import Sequence
from datetime import date
from itertools import combinations
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from ai_daily.config import Settings
from ai_daily.models import Digest, DigestItem


MAX_MARKDOWN_CHARS = 18_000
MAX_ATTEMPTS = 3
MARKDOWN_PUNCTUATION = re.compile(
    r"([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])"
)


class DingTalkError(RuntimeError):
    """A safe delivery error that never exposes request secrets."""


def build_webhook(settings: Settings) -> str:
    webhook = settings.dingtalk_webhook.get_secret_value()
    parsed = urlsplit(webhook)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    webhook_tokens = [value for key, value in query if key == "access_token"]
    if len(webhook_tokens) > 1:
        raise ValueError("webhook must contain exactly one nonblank access_token")
    if not webhook_tokens or not webhook_tokens[0].strip():
        query = [(key, value) for key, value in query if key != "access_token"]
        separate_token = settings.dingtalk_access_token
        token = (
            ""
            if separate_token is None
            else separate_token.get_secret_value().strip()
        )
        if not token:
            raise ValueError("webhook must contain exactly one nonblank access_token")
        query.append(("access_token", token))
    return urlunsplit(parsed._replace(query=urlencode(query)))


def _markdown_text(value: object) -> str:
    normalized = " ".join(str(value).split())
    return MARKDOWN_PUNCTUATION.sub(r"\\\1", normalized)


def _markdown_url(value: object) -> str:
    return quote(str(value), safe=":/?&=#%+,-._~")


def _item_block(number: int, item: DigestItem) -> str:
    return "\n".join(
        [
            f"### {number}. {_markdown_text(item.title)}",
            f"> 【类别】{_markdown_text(item.category)}  ",
            f"> 【来源】{_markdown_text(item.source)}",
            "",
            f"**发生了什么：** {_markdown_text(item.summary)}",
            "",
            f"**为什么重要：** {_markdown_text(item.impact)}",
            "",
            f"[查看原文]({_markdown_url(item.url)})",
        ]
    )


def _heading(report_date: date, part: int, total: int, report_title: str) -> str:
    suffix = "" if total == 1 else f"（{part}/{total}）"
    return f"# {_markdown_text(report_title)}｜{report_date.isoformat()}{suffix}"


def _compose_part(
    report_date: date,
    part: int,
    total: int,
    blocks: Sequence[str],
    overview: str | None,
    trends: Sequence[str] | None,
    window_hours: int,
    report_title: str,
    intro: str | None,
    scope_text: str | None,
) -> str:
    sections = [_heading(report_date, part, total, report_title)]
    if intro is not None:
        sections.append(_markdown_text(intro))
    if overview is not None:
        sections.append(f"## 今日概览\n\n{_markdown_text(overview)}")
    sections.extend(blocks)
    if trends is not None:
        trend_lines = "\n".join(f"- {_markdown_text(trend)}" for trend in trends)
        sections.append(f"## 趋势观察\n\n{trend_lines}")
        scope = scope_text or f"信息范围：最近 {window_hours} 小时"
        sections.append(_markdown_text(scope))
    return "\n\n".join(sections)


def _partitions(blocks: Sequence[str], total: int) -> list[list[Sequence[str]]]:
    if total == 1:
        return [[blocks]]
    boundaries = range(1, len(blocks))
    results: list[list[Sequence[str]]] = []
    for cuts in combinations(boundaries, total - 1):
        points = (0, *cuts, len(blocks))
        results.append(
            [blocks[points[index] : points[index + 1]] for index in range(total)]
        )
    return results


def render_digest(
    digest: Digest,
    report_date: date,
    window_hours: int,
    max_chars: int = MAX_MARKDOWN_CHARS,
    *,
    report_title: str = "AI 技术日报",
    intro: str | None = None,
    scope_text: str | None = None,
) -> list[str]:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    limit = min(max_chars, MAX_MARKDOWN_CHARS)
    blocks = [_item_block(number, item) for number, item in enumerate(digest.items, 1)]
    if any(len(block) > limit for block in blocks):
        raise ValueError("one complete item block exceeds max_chars")

    for total in range(1, len(blocks) + 1):
        for groups in _partitions(blocks, total):
            parts = [
                _compose_part(
                    report_date=report_date,
                    part=index + 1,
                    total=total,
                    blocks=group,
                    overview=digest.overview if index == 0 else None,
                    trends=digest.trends if index == total - 1 else None,
                    window_hours=window_hours,
                    report_title=report_title,
                    intro=intro if index == 0 else None,
                    scope_text=scope_text,
                )
                for index, group in enumerate(groups)
            ]
            if all(len(part) <= limit for part in parts):
                return parts

    raise ValueError("one complete item block cannot fit within max_chars")


def render_status_notice(
    report_date: date,
    max_chars: int = MAX_MARKDOWN_CHARS,
) -> list[str]:
    text = (
        f"# AI 技术日报｜{report_date.isoformat()}\n\n"
        "## 今日状态\n\n"
        "今日暂未获取到可靠的 AI 技术内容。为避免生成未经来源支持的信息，"
        "本次仅发送状态通知。"
    )
    if max_chars <= 0 or len(text) > min(max_chars, MAX_MARKDOWN_CHARS):
        raise ValueError("status notice exceeds max_chars")
    return [text]


class DingTalkSender:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._webhook = build_webhook(settings)

    async def send(self, parts: Sequence[str], title: str) -> None:
        for part in parts:
            await self._send_part(part, title)

    async def _send_part(self, text: str, title: str) -> None:
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": text},
        }
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self._client.post(
                    self._webhook,
                    json=payload,
                    timeout=20.0,
                )
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt == MAX_ATTEMPTS - 1:
                    raise DingTalkError("DingTalk delivery failed") from None
                await asyncio.sleep(attempt + 1)
                continue
            except httpx.RequestError:
                raise DingTalkError("DingTalk delivery failed") from None

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_ATTEMPTS - 1:
                    raise DingTalkError("DingTalk delivery failed")
                await asyncio.sleep(attempt + 1)
                continue
            if not response.is_success:
                raise DingTalkError("DingTalk delivery failed")

            try:
                result = response.json()
            except ValueError:
                raise DingTalkError("DingTalk returned an invalid response") from None
            errcode = result.get("errcode") if isinstance(result, dict) else None
            if type(errcode) is not int or errcode != 0:
                raise DingTalkError("DingTalk rejected the message")
            return
