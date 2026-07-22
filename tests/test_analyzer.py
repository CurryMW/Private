import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from ai_daily.analyzer import AnalysisError, Analyzer
from ai_daily.config import Settings
from ai_daily.models import Candidate, Digest


ENDPOINT = "https://apiclaude.cc/v1/chat/completions"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        ai_api_key="sk-test",
        dingtalk_webhook=(
            "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        ),
    )


@pytest.fixture
def candidates() -> list[Candidate]:
    return [
        Candidate(
            id="candidate-0001",
            title="新的推理模型正式发布",
            summary="官方介绍了模型能力、技术细节和评测范围。",
            source="Official Lab",
            url="https://example.com/model-release?utm_source=newsletter",
            published_at=datetime(2026, 7, 17, 23, 30, tzinfo=UTC),
            source_kind="rss",
        )
    ]


def _item(
    *,
    url: str = "https://example.com/model-release",
    title: str = "新的推理模型正式发布",
) -> dict[str, str]:
    return {
        "title": title,
        "category": "模型发布",
        "source": "Official Lab",
        "summary": "官方发布了新的推理模型并介绍核心技术细节。",
        "impact": "这为开发者评估推理能力和部署选择提供了新依据。",
        "url": url,
    }


def _digest_payload(**overrides) -> dict:
    payload = {
        "overview": "今天的更新聚焦推理模型及其工程应用进展。",
        "items": [_item()],
        "trends": ["推理能力继续增强", "工程部署成为重点"],
    }
    payload.update(overrides)
    return payload


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1784334600,
            "model": "claude-sonnet-4-6",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 200,
                "total_tokens": 700,
            },
        },
    )


@pytest.mark.asyncio
@respx.mock
async def test_analyze_sends_chinese_openai_compatible_request_and_returns_digest(
    settings: Settings, candidates: list[Candidate]
) -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=_completion(json.dumps(_digest_payload(), ensure_ascii=False))
    )

    async with httpx.AsyncClient() as client:
        digest = await Analyzer(client, settings).analyze(candidates)

    assert isinstance(digest, Digest)
    assert digest.items[0].title == "新的推理模型正式发布"
    request = route.calls[0].request
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-4-6"
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 3000
    assert body["messages"][0]["role"] == "system"
    assert "你是严谨的 AI 技术编辑" in body["messages"][0]["content"]
    assert body["messages"][1]["role"] == "user"
    user_message = body["messages"][1]["content"]
    assert "新的推理模型正式发布" in user_message
    assert "https://example.com/model-release?utm_source=newsletter" in user_message
    assert '"source_kind"' not in user_message
    assert '"properties"' in user_message
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert request.extensions["timeout"]["read"] == 180.0


@pytest.mark.asyncio
@respx.mock
async def test_analyze_strips_one_surrounding_json_fence(
    settings: Settings, candidates: list[Candidate]
) -> None:
    content = json.dumps(_digest_payload(), ensure_ascii=False)
    respx.post(ENDPOINT).mock(return_value=_completion(f"```json\n{content}\n```"))

    async with httpx.AsyncClient() as client:
        digest = await Analyzer(client, settings).analyze(candidates)

    assert digest.overview == "今天的更新聚焦推理模型及其工程应用进展。"


@pytest.mark.asyncio
@respx.mock
async def test_analyze_rejects_a_ninth_item_without_retrying(
    settings: Settings, candidates: list[Candidate], monkeypatch
) -> None:
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ai_daily.analyzer.asyncio.sleep", fake_sleep)
    payload = _digest_payload(
        items=[_item(title=f"新的推理模型正式发布 {index}") for index in range(9)]
    )
    route = respx.post(ENDPOINT).mock(
        return_value=_completion(json.dumps(payload, ensure_ascii=False))
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(AnalysisError, match="validation"):
            await Analyzer(client, settings).analyze(candidates)

    assert route.call_count == 1
    assert sleeps == []


@pytest.mark.asyncio
@respx.mock
async def test_analyze_enforces_the_configured_maximum(
    settings: Settings, candidates: list[Candidate]
) -> None:
    limited_settings = settings.model_copy(update={"max_items": 1})
    second_candidate = candidates[0].model_copy(
        update={
            "id": "candidate-0002",
            "url": "https://example.com/second-model",
        }
    )
    payload = _digest_payload(
        items=[_item(), _item(url="https://example.com/second-model")]
    )
    respx.post(ENDPOINT).mock(
        return_value=_completion(json.dumps(payload, ensure_ascii=False))
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(AnalysisError, match="maximum"):
            await Analyzer(client, limited_settings).analyze(
                candidates + [second_candidate]
            )


@pytest.mark.asyncio
@respx.mock
async def test_analyze_marks_invalid_digest_as_retryable_with_smaller_input(
    settings: Settings, candidates: list[Candidate]
) -> None:
    respx.post(ENDPOINT).mock(
        return_value=_completion(
            json.dumps(_digest_payload(overview=""), ensure_ascii=False)
        )
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(AnalysisError) as captured:
            await Analyzer(client, settings).analyze(candidates)

    assert captured.value.retry_with_smaller_input is True


@pytest.mark.asyncio
@respx.mock
async def test_analyze_prompts_for_and_enforces_per_run_maximum(
    settings: Settings, candidates: list[Candidate]
) -> None:
    expanded = [
        candidates[0].model_copy(
            update={
                "id": f"candidate-{index:04d}",
                "url": f"https://example.com/model-{index}",
            }
        )
        for index in range(1, 5)
    ]
    payload = _digest_payload(
        items=[
            _item(url=f"https://example.com/model-{index}", title=f"模型更新 {index}")
            for index in range(1, 5)
        ]
    )
    route = respx.post(ENDPOINT).mock(
        return_value=_completion(json.dumps(payload, ensure_ascii=False))
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(AnalysisError, match="maximum"):
            await Analyzer(client, settings).analyze(expanded, max_items=3)

    request_body = json.loads(route.calls[0].request.content)
    assert "本次最多选择 3 条" in request_body["messages"][1]["content"]


@pytest.mark.asyncio
@respx.mock
async def test_analyze_rejects_url_outside_candidate_evidence_without_retrying(
    settings: Settings, candidates: list[Candidate], monkeypatch
) -> None:
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ai_daily.analyzer.asyncio.sleep", fake_sleep)
    payload = _digest_payload(items=[_item(url="https://fabricated.example/story")])
    route = respx.post(ENDPOINT).mock(
        return_value=_completion(json.dumps(payload, ensure_ascii=False))
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(AnalysisError, match="evidence"):
            await Analyzer(client, settings).analyze(candidates)

    assert route.call_count == 1
    assert sleeps == []


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"overview": ""},
        {"trends": ["只有一个趋势"]},
    ],
)
async def test_analyze_rejects_invalid_digest_schema_without_retrying(
    invalid_fields: dict,
    settings: Settings,
    candidates: list[Candidate],
    monkeypatch,
) -> None:
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ai_daily.analyzer.asyncio.sleep", fake_sleep)
    route = respx.post(ENDPOINT).mock(
        return_value=_completion(
            json.dumps(_digest_payload(**invalid_fields), ensure_ascii=False)
        )
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(AnalysisError, match="validation"):
            await Analyzer(client, settings).analyze(candidates)

    assert route.call_count == 1
    assert sleeps == []


@pytest.mark.asyncio
@respx.mock
async def test_analyze_retries_transient_failures_at_most_three_attempts(
    settings: Settings, candidates: list[Candidate], monkeypatch
) -> None:
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ai_daily.analyzer.asyncio.sleep", fake_sleep)
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(503),
            _completion(json.dumps(_digest_payload(), ensure_ascii=False)),
        ]
    )

    async with httpx.AsyncClient() as client:
        digest = await Analyzer(client, settings).analyze(candidates)

    assert digest.items[0].source == "Official Lab"
    assert route.call_count == 3
    assert sleeps == [1, 2]


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("exception_type", [httpx.ConnectError, httpx.ReadTimeout])
async def test_analyze_retries_transient_transport_error(
    exception_type,
    settings: Settings,
    candidates: list[Candidate],
    monkeypatch,
) -> None:
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ai_daily.analyzer.asyncio.sleep", fake_sleep)
    request = httpx.Request("POST", ENDPOINT)
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            exception_type("temporary failure", request=request),
            _completion(json.dumps(_digest_payload(), ensure_ascii=False)),
        ]
    )

    async with httpx.AsyncClient() as client:
        digest = await Analyzer(client, settings).analyze(candidates)

    assert digest.items[0].source == "Official Lab"
    assert route.call_count == 2
    assert sleeps == [1]


@pytest.mark.asyncio
@respx.mock
async def test_analyze_stops_after_three_transient_attempts(
    settings: Settings, candidates: list[Candidate], monkeypatch
) -> None:
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ai_daily.analyzer.asyncio.sleep", fake_sleep)
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(502),
            httpx.Response(503, text="private response body"),
        ]
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(AnalysisError) as captured:
            await Analyzer(client, settings).analyze(candidates)

    assert route.call_count == 3
    assert sleeps == [1, 2]
    assert "HTTP 503" in str(captured.value)
    assert "private response body" not in str(captured.value)


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    ("exception_type", "expected_message"),
    [
        (httpx.ReadTimeout, "timed out after 3 attempts"),
        (httpx.ConnectError, "connection failed after 3 attempts"),
    ],
)
async def test_analyze_classifies_terminal_transport_error_without_details(
    exception_type,
    expected_message: str,
    settings: Settings,
    candidates: list[Candidate],
    monkeypatch,
) -> None:
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ai_daily.analyzer.asyncio.sleep", fake_sleep)
    request = httpx.Request("POST", ENDPOINT)
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            exception_type("private upstream detail", request=request)
            for _ in range(3)
        ]
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(AnalysisError) as captured:
            await Analyzer(client, settings).analyze(candidates)

    assert route.call_count == 3
    assert sleeps == [1, 2]
    assert expected_message in str(captured.value)
    assert "private upstream detail" not in str(captured.value)


@pytest.mark.asyncio
@respx.mock
async def test_analyze_classifies_terminal_rate_limit(
    settings: Settings, candidates: list[Candidate], monkeypatch
) -> None:
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ai_daily.analyzer.asyncio.sleep", fake_sleep)
    route = respx.post(ENDPOINT).mock(
        side_effect=[httpx.Response(429, text="private response body") for _ in range(3)]
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(AnalysisError) as captured:
            await Analyzer(client, settings).analyze(candidates)

    assert route.call_count == 3
    assert sleeps == [1, 2]
    assert "rate limited after 3 attempts" in str(captured.value)
    assert "private response body" not in str(captured.value)


@pytest.mark.asyncio
@respx.mock
async def test_analyze_does_not_retry_status_outside_429_or_5xx(
    settings: Settings, candidates: list[Candidate], monkeypatch
) -> None:
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ai_daily.analyzer.asyncio.sleep", fake_sleep)
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(600))

    async with httpx.AsyncClient() as client:
        with pytest.raises(AnalysisError, match="HTTP 600"):
            await Analyzer(client, settings).analyze(candidates)

    assert route.call_count == 1
    assert sleeps == []


@pytest.mark.asyncio
@respx.mock
async def test_analysis_errors_do_not_expose_response_body(
    settings: Settings, candidates: list[Candidate]
) -> None:
    secret_body = "private model response token: do-not-expose"
    respx.post(ENDPOINT).mock(return_value=httpx.Response(400, text=secret_body))

    async with httpx.AsyncClient() as client:
        with pytest.raises(AnalysisError) as captured:
            await Analyzer(client, settings).analyze(candidates)

    assert secret_body not in str(captured.value)
    assert "do-not-expose" not in str(captured.value)
    assert captured.value.retry_with_smaller_input is False


@pytest.mark.asyncio
@respx.mock
async def test_analyze_wraps_nonretryable_request_error_without_details(
    settings: Settings, candidates: list[Candidate]
) -> None:
    request = httpx.Request("POST", ENDPOINT)
    route = respx.post(ENDPOINT).mock(
        side_effect=httpx.ReadError(
            "private transport detail",
            request=request,
        )
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(AnalysisError) as captured:
            await Analyzer(client, settings).analyze(candidates)

    assert route.call_count == 1
    assert str(captured.value) == "AI analysis request failed"
    assert captured.value.retry_with_smaller_input is False
    assert captured.value.__cause__ is None
