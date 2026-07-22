# Model Failure Delivery Implementation Plan

**Goal:** Limit model inputs and send a safe DingTalk notification when analysis cannot return a validated digest.

**Architecture:** Add initial and retry candidate limits to Settings. Classify only digest validation failures as retryable. The application uses a bounded candidate subset, retries once with a smaller subset, then uses a fixed model-service notification through the existing DingTalk flow.

**Tech Stack:** Python 3.12, Pydantic 2, pytest, HTTPX, DingTalk Markdown.

## Constraints

- Initial model candidate limit is 12.
- Reduced retry candidate limit is 6.
- HTTP 403 and every non-validation analysis failure does not retry.
- Model fallback saves daily delivery state only.
- Production code follows a failed-test-first cycle.

### Task 1: Settings and analysis classification

Files: src/ai_daily/config.py, tests/test_config.py, src/ai_daily/analyzer.py, tests/test_analyzer.py.

First add these test assertions:

~~~python
assert settings.model_candidate_limit == 12
assert settings.model_retry_candidate_limit == 6

def test_model_candidate_limits_are_validated() -> None:
    with pytest.raises(ValueError, match="model candidate limit"):
        load_settings(
            BASE_ENV | {"MAX_ITEMS": "8", "MODEL_CANDIDATE_LIMIT": "7"}
        )
    with pytest.raises(ValueError, match="model retry candidate limit"):
        load_settings(
            BASE_ENV
            | {
                "MODEL_CANDIDATE_LIMIT": "12",
                "MODEL_RETRY_CANDIDATE_LIMIT": "13",
            }
        )
~~~

~~~python
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
~~~

Run:

~~~powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_analyzer.py::test_analyze_marks_invalid_digest_as_retryable_with_smaller_input -q
~~~

Expected: missing attributes.

Implement:

~~~python
model_candidate_limit: int = Field(default=12, gt=0)
model_retry_candidate_limit: int = Field(default=6, gt=0)

if self.model_candidate_limit < self.max_items:
    raise ValueError("model candidate limit must cover max items")
if self.model_retry_candidate_limit > self.model_candidate_limit:
    raise ValueError("model retry candidate limit must not exceed initial limit")
~~~

~~~python
class AnalysisError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retry_with_smaller_input: bool = False,
    ) -> None:
        super().__init__(message)
        self.retry_with_smaller_input = retry_with_smaller_input
~~~

Change only digest model validation to:

~~~python
raise AnalysisError(
    "analysis validation failed",
    retry_with_smaller_input=True,
) from None
~~~

Verify and commit:

~~~powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_analyzer.py -q
git add -- src/ai_daily/config.py tests/test_config.py src/ai_daily/analyzer.py tests/test_analyzer.py
git commit -m "feat: configure retryable model analysis"
~~~

### Task 2: Fixed model-service notice

Files: src/ai_daily/dingtalk.py, tests/test_dingtalk.py.

First import the renderer and add:

~~~python
def test_render_model_service_notice_is_fixed_and_safe() -> None:
    assert render_model_service_notice(date(2026, 7, 22)) == [
        "# AI 技术日报｜2026-07-22\n\n"
        "## 模型服务状态\n\n"
        "今日的可靠候选内容已收集，但摘要生成服务暂时不可用。"
        "为避免发送未经校验的内容，本次仅发送状态通知。"
    ]
~~~

Run:

~~~powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_dingtalk.py::test_render_model_service_notice_is_fixed_and_safe -q
~~~

Expected: import failure.

Implement:

~~~python
def render_model_service_notice(
    report_date: date,
    max_chars: int = MAX_MARKDOWN_CHARS,
) -> list[str]:
    text = (
        f"# AI 技术日报｜{report_date.isoformat()}\n\n"
        "## 模型服务状态\n\n"
        "今日的可靠候选内容已收集，但摘要生成服务暂时不可用。"
        "为避免发送未经校验的内容，本次仅发送状态通知。"
    )
    if max_chars <= 0 or len(text) > min(max_chars, MAX_MARKDOWN_CHARS):
        raise ValueError("model service notice exceeds max_chars")
    return [text]
~~~

Verify and commit:

~~~powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_dingtalk.py -q
git add -- src/ai_daily/dingtalk.py tests/test_dingtalk.py
git commit -m "feat: render model service status notice"
~~~

### Task 3: Bounded analysis and delivery fallback

Files: src/ai_daily/app.py, tests/test_app.py, README.md.

First add a test with 13 fresh candidates whose analyzer spy raises:

~~~python
AnalysisError(
    "analysis validation failed",
    retry_with_smaller_input=True,
)
~~~

The spy must assert these exact calls:

~~~python
assert calls == [(candidates[:12], 8), (candidates[:6], 8)]
~~~

Add a separate HTTP 403 analyzer spy test that asserts a one-part model notice is sent, no URL state file exists, and DeliveryState contains the report date.

Run:

~~~powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_app.py -q
~~~

Expected: the original analysis input is unbounded and HTTP 403 propagates.

Implement this analysis control flow:

~~~python
model_candidates = batch.candidates[: settings.model_candidate_limit]
analyzer = Analyzer(client, settings)
try:
    digest = await analyzer.analyze(model_candidates, max_items=batch.max_items)
except AnalysisError as error:
    retry_candidates = model_candidates[: settings.model_retry_candidate_limit]
    if error.retry_with_smaller_input and len(retry_candidates) < len(model_candidates):
        try:
            digest = await analyzer.analyze(
                retry_candidates,
                max_items=batch.max_items,
            )
        except AnalysisError:
            digest = None
    else:
        digest = None
~~~

When digest is None, set:

~~~python
parts = render_model_service_notice(report_date)
selected_count = 0
~~~

Only render a digest and save URLs when digest is non-None. Update the Chinese README with the two environment settings and an explanation that model HTTP 403, timeouts, and invalid JSON produce a model-service status notification.

Verify, commit, push, and check CI:

~~~powershell
& '.venv\Scripts\python.exe' -m pytest -q
git diff --check
git add -- src/ai_daily/app.py tests/test_app.py README.md
git commit -m "fix: deliver status notice when model analysis fails"
git push origin master
~~~

Expected: all tests pass, diff check is clean, and the pushed Test workflow succeeds. Do not dispatch a live DingTalk run without a separate approval.
