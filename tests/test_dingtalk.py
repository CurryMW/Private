import json
from datetime import date
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
import respx
from pydantic import SecretStr

from ai_daily.config import Settings
from ai_daily.dingtalk import (
    DingTalkError,
    DingTalkSender,
    build_webhook,
    render_digest,
)
from ai_daily.models import Category, Digest, DigestItem


FULL_WEBHOOK = (
    "https://oapi.dingtalk.com/robot/send?access_token=full-token&source=daily"
)
BASE_WEBHOOK = "https://oapi.dingtalk.com/robot/send"


def _settings(
    webhook: str = FULL_WEBHOOK,
    access_token: str | None = None,
) -> Settings:
    return Settings(
        ai_api_key="sk-test",
        dingtalk_webhook=webhook,
        dingtalk_access_token=access_token,
    )


def _digest(item_count: int = 2, *, verbose: bool = False) -> Digest:
    summary = (
        "项目发布了新的推理能力，并公开了完整的技术说明与使用方式。"
        if not verbose
        else "项目发布了新的推理能力，" * 12
    )
    impact = (
        "这让研发团队可以更可靠地评估并采用相关技术。"
        if not verbose
        else "这让研发团队可以更可靠地评估并采用相关技术。" * 10
    )
    return Digest(
        overview="今天的重要进展集中在模型推理和开源开发工具两个方向。",
        items=[
            DigestItem(
                title=f"重要技术进展 {index}",
                category=Category.MODEL if index == 1 else Category.OPEN_SOURCE,
                source=f"官方来源 {index}",
                summary=summary,
                impact=impact,
                url=f"https://example.com/reports/{index}?ref=official",
            )
            for index in range(1, item_count + 1)
        ],
        trends=["推理效率继续提升", "开源工具更加重视生产可用性"],
    )


def test_build_webhook_preserves_existing_access_token_exactly_once() -> None:
    webhook = build_webhook(_settings(access_token="ignored-token"))

    assert parse_qsl(urlsplit(webhook).query, keep_blank_values=True) == [
        ("access_token", "full-token"),
        ("source", "daily"),
    ]


def test_build_webhook_adds_separate_token_with_url_encoding() -> None:
    webhook = build_webhook(_settings(BASE_WEBHOOK, "separate token+/="))

    assert parse_qsl(urlsplit(webhook).query, keep_blank_values=True) == [
        ("access_token", "separate token+/="),
    ]


def test_build_webhook_rejects_multiple_access_token_parameters() -> None:
    settings = _settings(
        f"{BASE_WEBHOOK}?access_token=first&source=daily&access_token=second"
    )

    with pytest.raises(ValueError, match="exactly one nonblank access_token"):
        build_webhook(settings)


def test_build_webhook_replaces_blank_token_with_separate_token() -> None:
    settings = _settings(f"{BASE_WEBHOOK}?access_token=&source=daily", "replacement")

    webhook = build_webhook(settings)

    assert parse_qsl(urlsplit(webhook).query, keep_blank_values=True) == [
        ("source", "daily"),
        ("access_token", "replacement"),
    ]


def test_build_webhook_rejects_when_no_token_is_usable() -> None:
    settings = _settings().model_copy(
        update={
            "dingtalk_webhook": SecretStr(f"{BASE_WEBHOOK}?access_token=%20"),
            "dingtalk_access_token": SecretStr("   "),
        }
    )

    with pytest.raises(ValueError, match="exactly one nonblank access_token"):
        build_webhook(settings)


def test_render_digest_contains_required_chinese_markdown() -> None:
    parts = render_digest(_digest(), date(2026, 7, 18), window_hours=36)

    assert len(parts) == 1
    text = parts[0]
    assert "AI 技术日报｜2026-07-18" in text
    assert "今日概览" in text
    assert "【类别】模型发布" in text
    assert "【来源】官方来源 1" in text
    assert "**发生了什么：**" in text
    assert "**为什么重要：**" in text
    assert "[查看原文](https://example.com/reports/1?ref=official)" in text
    assert "趋势观察" in text
    assert "信息范围：最近 36 小时" in text


def test_render_digest_neutralizes_markdown_in_every_controlled_text_field() -> None:
    injected = "\n# 注入标题 [恶意链接](https://evil.example)"
    original = _digest(item_count=1)
    item = original.items[0].model_copy(
        update={
            "title": f"正常标题{injected}",
            "category": f"模型发布{injected}",
            "source": f"官方来源{injected}",
            "summary": f"正常摘要内容足够长{injected}",
            "impact": f"正常影响内容足够长{injected}",
        }
    )
    digest = original.model_copy(
        update={
            "overview": f"正常概览内容足够长{injected}",
            "items": [item],
            "trends": [
                f"正常趋势一{injected}",
                f"正常趋势二{injected}",
            ],
        }
    )

    text = render_digest(digest, date(2026, 7, 18), 36)[0]

    assert [line for line in text.splitlines() if line.startswith("#")] == [
        "# AI 技术日报｜2026-07-18",
        "## 今日概览",
        "### 1. 正常标题 \\# 注入标题 \\[恶意链接\\]\\(https\\:\\/\\/evil\\.example\\)",
        "## 趋势观察",
    ]
    assert text.count("\\# 注入标题") == 8
    assert "[恶意链接](https://evil.example)" not in text
    assert "\n# 注入标题" not in text


def test_render_digest_neutralizes_plain_text_autolinks() -> None:
    digest = _digest(item_count=1).model_copy(
        update={"overview": "正常概览内容 https://localhost/path"}
    )

    text = render_digest(digest, date(2026, 7, 18), 36)[0]

    assert "https://localhost/path" not in text
    assert "https\\:\\/\\/localhost\\/path" in text


def test_render_digest_percent_encodes_markdown_link_destination_delimiters() -> None:
    digest = _digest(item_count=1)
    item = digest.items[0].model_copy(
        update={
            "url": "https://example.com/report_(final)?version=(2)&source=official"
        }
    )
    digest = digest.model_copy(update={"items": [item]})

    text = render_digest(digest, date(2026, 7, 18), 36)[0]

    assert (
        "[查看原文](https://example.com/report_%28final%29?version=%282%29&source=official)"
        in text
    )
    assert "report_(final)" not in text


def test_render_digest_splits_only_at_item_boundaries() -> None:
    digest = _digest(verbose=True)

    parts = render_digest(digest, date(2026, 7, 18), 36, max_chars=1_000)

    assert len(parts) == 2
    assert all(len(part) <= 1_000 for part in parts)
    assert "AI 技术日报｜2026-07-18（1/2）" in parts[0]
    assert "AI 技术日报｜2026-07-18（2/2）" in parts[1]
    assert "今日概览" in parts[0]
    assert "今日概览" not in parts[1]
    assert "趋势观察" not in parts[0]
    assert "趋势观察" in parts[1]
    assert "### 1. 重要技术进展 1" in parts[0]
    assert "### 1. 重要技术进展 1" not in parts[1]
    assert "### 2. 重要技术进展 2" not in parts[0]
    assert "### 2. 重要技术进展 2" in parts[1]
    for item in digest.items:
        item_url = str(item.url)
        containing_parts = [part for part in parts if item_url in part]
        assert len(containing_parts) == 1
        assert item.summary in containing_parts[0]
        assert item.impact in containing_parts[0]


def test_render_digest_rejects_an_item_too_large_for_one_part() -> None:
    with pytest.raises(ValueError, match="item block"):
        render_digest(_digest(item_count=1), date(2026, 7, 18), 36, max_chars=100)


def test_render_digest_never_exceeds_the_production_limit() -> None:
    digest = _digest(item_count=1).model_copy(
        update={"trends": ["趋" * 18_000, "开源生态持续成熟"]}
    )

    with pytest.raises(ValueError):
        render_digest(digest, date(2026, 7, 18), 36, max_chars=50_000)


@pytest.mark.asyncio
@respx.mock
async def test_sender_posts_exact_unsigned_markdown_payload_sequentially() -> None:
    route = respx.post(FULL_WEBHOOK).mock(
        side_effect=[
            httpx.Response(200, json={"errcode": 0, "errmsg": "ok"}),
            httpx.Response(200, json={"errcode": 0, "errmsg": "ok"}),
        ]
    )

    async with httpx.AsyncClient() as client:
        await DingTalkSender(client, _settings()).send(["第一部分", "第二部分"], "日报")

    assert [json.loads(call.request.content) for call in route.calls] == [
        {"msgtype": "markdown", "markdown": {"title": "日报", "text": "第一部分"}},
        {"msgtype": "markdown", "markdown": {"title": "日报", "text": "第二部分"}},
    ]


@pytest.mark.asyncio
@respx.mock
async def test_sender_accepts_success_response() -> None:
    respx.post(FULL_WEBHOOK).mock(
        return_value=httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
    )

    async with httpx.AsyncClient() as client:
        await DingTalkSender(client, _settings()).send(["正文"], "日报")


@pytest.mark.asyncio
@respx.mock
async def test_sender_uses_static_safe_error_for_nonzero_errcode() -> None:
    secret_token = "full-token"
    leaked_text = f"bad request {FULL_WEBHOOK} payload-secret {secret_token}"
    route = respx.post(FULL_WEBHOOK).mock(
        return_value=httpx.Response(
            200,
            json={"errcode": 310000, "errmsg": leaked_text},
        )
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(DingTalkError) as raised:
            await DingTalkSender(client, _settings()).send(["payload-secret"], "日报")

    assert len(route.calls) == 1
    message = str(raised.value)
    assert message == "DingTalk rejected the message"
    assert FULL_WEBHOOK not in message
    assert secret_token not in message
    assert "payload-secret" not in message


@pytest.mark.asyncio
@respx.mock
async def test_sender_retries_http_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[int] = []

    async def record_delay(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr("ai_daily.dingtalk.asyncio.sleep", record_delay)
    route = respx.post(FULL_WEBHOOK).mock(
        side_effect=[
            httpx.Response(429, json={"errcode": 1, "errmsg": "busy"}),
            httpx.Response(200, json={"errcode": 0, "errmsg": "ok"}),
        ]
    )

    async with httpx.AsyncClient() as client:
        await DingTalkSender(client, _settings()).send(["正文"], "日报")

    assert len(route.calls) == 2
    assert delays == [1]


@pytest.mark.asyncio
@respx.mock
async def test_sender_caps_transient_failures_at_three_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_delay(_: int) -> None:
        return None

    monkeypatch.setattr("ai_daily.dingtalk.asyncio.sleep", no_delay)
    route = respx.post(FULL_WEBHOOK).mock(
        return_value=httpx.Response(503, json={"errcode": 1, "errmsg": "busy"})
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(DingTalkError, match="DingTalk delivery failed"):
            await DingTalkSender(client, _settings()).send(["正文"], "日报")

    assert len(route.calls) == 3


@pytest.mark.asyncio
@respx.mock
async def test_sender_does_not_retry_nontransient_http_error() -> None:
    route = respx.post(FULL_WEBHOOK).mock(
        return_value=httpx.Response(400, json={"errcode": 400, "errmsg": "bad"})
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(DingTalkError, match="DingTalk delivery failed"):
            await DingTalkSender(client, _settings()).send(["正文"], "日报")

    assert len(route.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_sender_rejects_redirect_even_with_success_shaped_json() -> None:
    route = respx.post(FULL_WEBHOOK).mock(
        return_value=httpx.Response(302, json={"errcode": 0, "errmsg": "ok"})
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(DingTalkError, match="DingTalk delivery failed"):
            await DingTalkSender(client, _settings()).send(["正文"], "日报")

    assert len(route.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_sender_does_not_retry_status_outside_http_5xx() -> None:
    route = respx.post(FULL_WEBHOOK).mock(
        return_value=httpx.Response(600, json={"errcode": 600, "errmsg": "bad"})
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(DingTalkError, match="DingTalk delivery failed"):
            await DingTalkSender(client, _settings()).send(["正文"], "日报")

    assert len(route.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_sender_removes_secret_bearing_network_error_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_delay(_: int) -> None:
        return None

    monkeypatch.setattr("ai_daily.dingtalk.asyncio.sleep", no_delay)
    secret_error = httpx.ConnectError(
        "failed",
        request=httpx.Request("POST", FULL_WEBHOOK),
    )
    respx.post(FULL_WEBHOOK).mock(side_effect=secret_error)

    async with httpx.AsyncClient() as client:
        with pytest.raises(DingTalkError) as raised:
            await DingTalkSender(client, _settings()).send(["正文"], "日报")

    assert str(raised.value) == "DingTalk delivery failed"
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
@respx.mock
async def test_sender_does_not_retry_nontransient_request_error() -> None:
    secret_error = httpx.ReadError(
        "failed",
        request=httpx.Request("POST", FULL_WEBHOOK),
    )
    route = respx.post(FULL_WEBHOOK).mock(side_effect=secret_error)

    async with httpx.AsyncClient() as client:
        with pytest.raises(DingTalkError) as raised:
            await DingTalkSender(client, _settings()).send(["正文"], "日报")

    assert len(route.calls) == 1
    assert str(raised.value) == "DingTalk delivery failed"
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("errcode", [False, 0.0, "0"])
async def test_sender_requires_integer_zero_errcode(errcode: object) -> None:
    route = respx.post(FULL_WEBHOOK).mock(
        return_value=httpx.Response(200, json={"errcode": errcode, "errmsg": "ok"})
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(DingTalkError, match="DingTalk rejected the message"):
            await DingTalkSender(client, _settings()).send(["正文"], "日报")

    assert len(route.calls) == 1
