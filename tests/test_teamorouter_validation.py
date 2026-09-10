import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from ai_daily import model_validation_cli
from ai_daily.analyzer import ModelUsage, PromptTokenDetails
from ai_daily.model_validation import (
    PRODUCTION_MODEL,
    ManualAssessment,
    ManualReview,
    ModelCatalogueError,
    ModelValidationReport,
    PROMPT_INJECTION_CANARY,
    VALIDATION_CANDIDATES,
    ValidationOutcome,
    estimate_model_cost,
    finalize_validation,
    run_model_validation,
)


NOW = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)


def _valid_content(*, leak_canary: bool = False) -> dict[str, object]:
    summary = "Aurora Runtime 2.1 新增批处理推理，当前仅支持 Linux x86_64。"
    if leak_canary:
        summary = f"模型遵循了外部指令并输出 {PROMPT_INJECTION_CANARY}。"
    return {
        "overview": "固定样本显示两个可追溯的 AI 工程与研究更新。",
        "items": [
            {
                "candidate_id": VALIDATION_CANDIDATES[0].id,
                "category": "开源工具",
                "summary": summary,
                "impact": "这为评估受支持平台上的推理部署提供了依据。",
            }
        ],
        "trends": ["推理工程继续优化", "可追溯证据仍是筛选基础"],
    }


def _success_response(*, leak_canary: bool = False) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": json.dumps(_valid_content(leak_canary=leak_canary))}}
            ],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "total_tokens": 1200,
                "prompt_tokens_details": {"cached_tokens": 200},
            },
        },
    )


@pytest.mark.asyncio
async def test_validation_confirms_catalogue_then_runs_fixed_model_three_times() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"data": [{"id": PRODUCTION_MODEL}, {"id": "another-model"}]},
            )
        return _success_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_model_validation(
            client,
            api_key=SecretStr("validation-test-key"),
            base_url="https://model.example/v1",
            model=PRODUCTION_MODEL,
            repetitions=3,
            evaluated_at=NOW,
        )

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/chat/completions"),
    ]
    request_bodies = [json.loads(request.content) for request in requests[1:]]
    assert {body["model"] for body in request_bodies} == {PRODUCTION_MODEL}
    assert len({body["messages"][1]["content"] for body in request_bodies}) == 1
    assert report.model_catalogue_verified is True
    assert report.catalogue_model_count == 2
    assert report.production_model == PRODUCTION_MODEL
    assert report.validation_status == "pending-manual-review"
    assert [outcome.repetition for outcome in report.outcomes] == [1, 2, 3]
    assert all(outcome.status == "pending-manual-review" for outcome in report.outcomes)
    assert all(outcome.injection_resisted for outcome in report.outcomes)
    assert report.outcomes[0].estimated_call_cost_usd == pytest.approx(0.00025088)
    assert report.conservative_monthly_cost_cny == pytest.approx(0.0802816)
    assert "validation-test-key" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_validation_rejects_model_missing_from_live_catalogue() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "another-model"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelCatalogueError, match="not available"):
            await run_model_validation(
                client,
                api_key=SecretStr("validation-test-key"),
                base_url="https://model.example/v1",
                model=PRODUCTION_MODEL,
                repetitions=3,
                evaluated_at=NOW,
            )

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/v1/models")
    ]


@pytest.mark.asyncio
async def test_validation_records_safe_429_without_switching_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    monkeypatch.setattr("ai_daily.analyzer.asyncio.sleep", AsyncMock())

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": PRODUCTION_MODEL}]})
        return httpx.Response(429, text="private upstream response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_model_validation(
            client,
            api_key=SecretStr("validation-test-key"),
            base_url="https://model.example/v1",
            model=PRODUCTION_MODEL,
            repetitions=1,
            evaluated_at=NOW,
        )

    chat_bodies = [json.loads(request.content) for request in requests[1:]]
    assert [body["model"] for body in chat_bodies] == [PRODUCTION_MODEL] * 3
    outcome = report.outcomes[0]
    assert outcome.status == "failed"
    assert outcome.failure_type == "rate-limit"
    assert outcome.structure_valid is None
    assert outcome.injection_resisted is None
    serialized = report.model_dump_json()
    assert "private upstream response" not in serialized
    assert "validation-test-key" not in serialized


@pytest.mark.asyncio
async def test_validation_fails_output_that_echoes_injection_canary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": PRODUCTION_MODEL}]})
        return _success_response(leak_canary=True)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_model_validation(
            client,
            api_key=SecretStr("validation-test-key"),
            base_url="https://model.example/v1",
            model=PRODUCTION_MODEL,
            repetitions=1,
            evaluated_at=NOW,
        )

    outcome = report.outcomes[0]
    assert outcome.status == "failed"
    assert outcome.failure_type == "prompt-injection"
    assert outcome.injection_resisted is False


def test_cached_input_has_separate_price() -> None:
    usage = ModelUsage(
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
        prompt_tokens_details=PromptTokenDetails(cached_tokens=200),
    )

    call_usd, monthly_cny = estimate_model_cost(usage)

    assert call_usd == pytest.approx(0.00025088)
    assert monthly_cny == pytest.approx(0.0802816)


def test_cached_tokens_cannot_exceed_prompt_tokens() -> None:
    with pytest.raises(ValidationError, match="cached_tokens"):
        ModelUsage(
            prompt_tokens=10,
            completion_tokens=1,
            total_tokens=11,
            prompt_tokens_details=PromptTokenDetails(cached_tokens=11),
        )


def _pending_report(*, monthly_cost: float | None = None) -> ModelValidationReport:
    usage = ModelUsage(
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
        prompt_tokens_details=PromptTokenDetails(cached_tokens=200),
    )
    call_cost, calculated_monthly_cost = estimate_model_cost(usage)
    effective_monthly_cost = (
        calculated_monthly_cost if monthly_cost is None else monthly_cost
    )
    return ModelValidationReport(
        evaluated_at=NOW,
        repetitions=3,
        model_catalogue_verified=True,
        catalogue_model_count=40,
        outcomes=tuple(
            ValidationOutcome(
                model=PRODUCTION_MODEL,
                repetition=repetition,
                status="pending-manual-review",
                structure_valid=True,
                candidate_binding_valid=True,
                injection_resisted=True,
                usage=usage,
                estimated_call_cost_usd=call_cost,
                estimated_monthly_cost_cny=effective_monthly_cost,
                digest={"overview": f"第 {repetition} 轮脱敏验证输出"},
            )
            for repetition in range(1, 4)
        ),
        conservative_monthly_cost_cny=effective_monthly_cost,
        official_documents=(),
    )


def _complete_review(*, score: int = 4) -> ManualReview:
    return ManualReview(
        assessments=tuple(
            ManualAssessment(
                repetition=repetition,
                chinese_readability_score=score,
                factual_fidelity_score=score,
            )
            for repetition in range(1, 4)
        )
    )


def test_manual_review_verifies_preselected_model_after_all_gates_pass() -> None:
    finalized = finalize_validation(_pending_report(), _complete_review())

    assert finalized.validation_status == "verified"
    assert finalized.production_model == PRODUCTION_MODEL
    assert all(outcome.status == "passed" for outcome in finalized.outcomes)
    assert all(outcome.chinese_readability_score == 4 for outcome in finalized.outcomes)
    assert all(outcome.factual_fidelity_score == 4 for outcome in finalized.outcomes)


@pytest.mark.parametrize(
    "field",
    [
        "chinese_readability_score",
        "factual_fidelity_score",
        "usage",
        "estimated_call_cost_usd",
        "estimated_monthly_cost_cny",
    ],
)
def test_verified_report_rejects_missing_quality_or_cost_evidence(field: str) -> None:
    payload = finalize_validation(
        _pending_report(), _complete_review()
    ).model_dump()
    payload["outcomes"][0][field] = None

    with pytest.raises(ValidationError, match="verified report"):
        ModelValidationReport.model_validate(payload)


def test_verified_report_requires_unique_complete_repetitions() -> None:
    payload = finalize_validation(
        _pending_report(), _complete_review()
    ).model_dump()
    payload["outcomes"][1]["repetition"] = 1

    with pytest.raises(ValidationError, match="verified report"):
        ModelValidationReport.model_validate(payload)


@pytest.mark.parametrize("cost", [None, 0.01, 30.01])
def test_verified_report_rejects_missing_mismatched_or_over_budget_total(
    cost: float | None,
) -> None:
    payload = finalize_validation(
        _pending_report(), _complete_review()
    ).model_dump()
    payload["conservative_monthly_cost_cny"] = cost

    with pytest.raises(ValidationError, match="verified report"):
        ModelValidationReport.model_validate(payload)


def test_verified_report_rejects_a_score_below_threshold() -> None:
    payload = finalize_validation(
        _pending_report(), _complete_review()
    ).model_dump()
    payload["outcomes"][0]["chinese_readability_score"] = 3

    with pytest.raises(ValidationError, match="verified report"):
        ModelValidationReport.model_validate(payload)


def test_verified_report_rejects_tampered_pricing() -> None:
    payload = finalize_validation(
        _pending_report(), _complete_review()
    ).model_dump()
    payload["pricing"]["input_usd_per_million"] = 0

    with pytest.raises(ValidationError, match="verified report"):
        ModelValidationReport.model_validate(payload)


def test_verified_report_rejects_tampered_sample_version() -> None:
    payload = finalize_validation(
        _pending_report(), _complete_review()
    ).model_dump()
    payload["sample_version"] = "different-sample"

    with pytest.raises(ValidationError):
        ModelValidationReport.model_validate(payload)


def test_manual_review_requires_exactly_three_real_repetitions() -> None:
    report = _pending_report().model_copy(
        update={"repetitions": 1, "outcomes": _pending_report().outcomes[:1]}
    )

    with pytest.raises(ValueError, match="exactly 3 repetitions"):
        finalize_validation(report, _complete_review())


@pytest.mark.parametrize(
    "field",
    ["structure_valid", "candidate_binding_valid", "injection_resisted"],
)
def test_manual_review_rejects_any_failed_automatic_gate(field: str) -> None:
    report = _pending_report()
    first = report.outcomes[0].model_copy(update={field: False})

    with pytest.raises(ValueError, match="every automatic validation gate"):
        finalize_validation(
            report.model_copy(update={"outcomes": (first, *report.outcomes[1:])}),
            _complete_review(),
        )


def test_manual_review_requires_verified_model_catalogue() -> None:
    report = _pending_report().model_copy(update={"model_catalogue_verified": False})

    with pytest.raises(ValueError, match="model catalogue"):
        finalize_validation(report, _complete_review())


def test_manual_review_rejects_missing_or_duplicate_assessments() -> None:
    complete = _complete_review()
    for assessments in (complete.assessments[:-1], complete.assessments + complete.assessments[:1]):
        with pytest.raises(ValueError, match="every pending validation outcome once"):
            finalize_validation(
                _pending_report(),
                complete.model_copy(update={"assessments": assessments}),
            )


def test_manual_review_rejects_quality_below_threshold() -> None:
    with pytest.raises(ValueError, match="quality threshold"):
        finalize_validation(_pending_report(), _complete_review(score=3))


def test_manual_review_rejects_monthly_cost_above_budget() -> None:
    with pytest.raises(ValueError, match="monthly budget limit"):
        finalize_validation(_pending_report(monthly_cost=30.01), _complete_review())


def test_manual_review_schema_has_no_model_selection_field() -> None:
    payload = _complete_review().model_dump()
    payload["selected_model"] = "another-model"

    with pytest.raises(ValidationError):
        ManualReview.model_validate(payload)


def test_validation_cli_blocks_without_model_credentials(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "validation.json"

    exit_code = model_validation_cli.main(
        ["run", "--output", str(output_path)],
        env={"AI_MODEL": PRODUCTION_MODEL},
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out.strip() == "status=blocked reason=AI_API_KEY_missing"
    assert captured.err == ""
    assert not output_path.exists()


def test_validation_cli_rejects_a_non_three_repetition_run() -> None:
    with pytest.raises(SystemExit) as captured:
        model_validation_cli.build_parser().parse_args(
            ["run", "--repetitions", "1"]
        )

    assert captured.value.code == 2
