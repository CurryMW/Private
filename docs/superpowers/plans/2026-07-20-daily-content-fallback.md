# Daily Content Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every successful scheduled run send one evidence-based DingTalk message by selecting fresh content, widening to seven days, replaying a labeled technical review, or sending a fixed status notice, while retaining only the 08:30 schedule.

**Architecture:** Collect configured sources once with a 168-hour cutoff, then use a pure local selector to choose `fresh`, `extended`, `review`, or `notice`. Keep evidence validation in the analyzer, add presentation options to the renderer, and let the existing app save URL state only for content messages and delivery-date state for every successfully delivered mode.

**Tech Stack:** Python 3.12, Pydantic 2, pytest/pytest-asyncio, GitHub Actions YAML, DingTalk Markdown.

## Global Constraints

- Prefer unseen content from the most recent 36 hours.
- Fall back to unseen content from the most recent 168 hours.
- If all seven-day candidates were already sent, allow a labeled review with at most 3 items.
- If no verifiable candidate exists, send a fixed status notice without calling the AI model.
- Never let the model use a URL outside the collected candidate evidence.
- Mark a Shanghai report date complete only after DingTalk accepts every message part.
- Retain only UTC cron `30 0 * * *`, corresponding to Asia/Shanghai 08:30.
- Do not add search API keys, databases, external schedulers, or new paid services.
- Write tests first and observe the expected failure before every production behavior change.

---

### Task 1: Pure four-level candidate selection

**Files:**
- Create: `src/ai_daily/selection.py`
- Create: `tests/test_selection.py`
- Modify: `src/ai_daily/config.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: collected `Candidate` objects, UTC `now`, `SentState`, primary/fallback window sizes, and the configured maximum item count.
- Produces: `CandidateBatch(mode, candidates, window_hours, max_items)` and `Settings.fallback_window_hours`.

- [ ] **Step 1: Write failing selector tests**

Create `tests/test_selection.py` with a candidate helper and these tests:

```python
from datetime import UTC, datetime, timedelta

from ai_daily.filtering import candidate_id
from ai_daily.models import Candidate
from ai_daily.selection import CandidateBatch, select_candidate_batch
from ai_daily.state import SentState


NOW = datetime(2026, 7, 20, 0, 30, tzinfo=UTC)


def candidate(url: str, *, hours_old: int, title: str = "AI model update") -> Candidate:
    return Candidate(
        id=candidate_id(url),
        title=title,
        summary="Technical model training and inference details.",
        source="Official Research",
        url=url,
        published_at=NOW - timedelta(hours=hours_old),
        source_kind="rss",
    )


def select(items, sent_state=None) -> CandidateBatch:
    return select_candidate_batch(
        items,
        now=NOW,
        sent_state=sent_state or SentState(),
        primary_window_hours=36,
        fallback_window_hours=168,
        max_items=8,
    )


def test_fresh_unseen_candidates_take_priority() -> None:
    fresh = candidate("https://example.com/fresh", hours_old=2)
    older = candidate("https://example.com/older", hours_old=72)

    batch = select([older, fresh])

    assert batch == CandidateBatch("fresh", [fresh], 36, 8)


def test_extended_uses_unseen_seven_day_candidates() -> None:
    older = candidate("https://example.com/older", hours_old=72)

    batch = select([older])

    assert batch == CandidateBatch("extended", [older], 168, 8)


def test_review_reuses_sent_candidates_but_keeps_quality_filtering() -> None:
    review = candidate("https://example.com/review", hours_old=72)
    business = candidate(
        "https://example.com/funding",
        hours_old=48,
        title="Startup funding and valuation announcement",
    ).model_copy(update={"summary": "Funding valuation and stock news."})
    state = SentState()
    state.mark_sent([str(review.url), str(business.url)], NOW - timedelta(hours=1))

    batch = select([business, review], state)

    assert batch == CandidateBatch("review", [review], 168, 3)


def test_notice_when_seven_day_window_has_no_usable_candidate() -> None:
    stale = candidate("https://example.com/stale", hours_old=169)

    batch = select([stale])

    assert batch == CandidateBatch("notice", [], 168, 0)
```

- [ ] **Step 2: Run selector tests and verify RED**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_selection.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'ai_daily.selection'`.

- [ ] **Step 3: Implement the selector**

Create `src/ai_daily/selection.py`:

```python
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from ai_daily.filtering import prepare_candidates
from ai_daily.models import Candidate
from ai_daily.state import SentState


BatchMode = Literal["fresh", "extended", "review", "notice"]


@dataclass(frozen=True)
class CandidateBatch:
    mode: BatchMode
    candidates: list[Candidate]
    window_hours: int
    max_items: int


def select_candidate_batch(
    candidates: list[Candidate],
    *,
    now: datetime,
    sent_state: SentState,
    primary_window_hours: int,
    fallback_window_hours: int,
    max_items: int,
) -> CandidateBatch:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    fresh = prepare_candidates(
        candidates,
        now - timedelta(hours=primary_window_hours),
        sent_state,
    )
    if fresh:
        return CandidateBatch("fresh", fresh, primary_window_hours, max_items)

    extended_cutoff = now - timedelta(hours=fallback_window_hours)
    extended = prepare_candidates(candidates, extended_cutoff, sent_state)
    if extended:
        return CandidateBatch("extended", extended, fallback_window_hours, max_items)

    review = prepare_candidates(candidates, extended_cutoff, SentState())
    if review:
        return CandidateBatch("review", review, fallback_window_hours, min(3, max_items))

    return CandidateBatch("notice", [], fallback_window_hours, 0)
```

- [ ] **Step 4: Run selector tests and verify GREEN**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_selection.py tests/test_filtering.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Write failing fallback-window configuration tests**

Add these assertions to `tests/test_config.py`:

```python
assert settings.fallback_window_hours == 168
```

Add `"FALLBACK_WINDOW_HOURS": "240"` to the custom environment test and assert:

```python
assert settings.fallback_window_hours == 240
```

Add a validation test:

```python
def test_fallback_window_must_cover_primary_window() -> None:
    with pytest.raises(ValueError, match="fallback window"):
        load_settings(
            BASE_ENV | {"WINDOW_HOURS": "72", "FALLBACK_WINDOW_HOURS": "48"}
        )
```

- [ ] **Step 6: Run configuration tests and verify RED**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_config.py -q
```

Expected: failures because `Settings` has no `fallback_window_hours` field.

- [ ] **Step 7: Implement fallback-window configuration**

Add to `Settings` in `src/ai_daily/config.py`:

```python
fallback_window_hours: int = Field(default=168, gt=0)
```

Add this validator:

```python
@model_validator(mode="after")
def validate_content_windows(self) -> "Settings":
    if self.fallback_window_hours < self.window_hours:
        raise ValueError("fallback window must cover primary window")
    return self
```

Pass the environment value from `load_settings`:

```python
fallback_window_hours=source.get("FALLBACK_WINDOW_HOURS", "168"),
```

- [ ] **Step 8: Run configuration and selector tests and verify GREEN**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_selection.py -q
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit candidate selection**

```powershell
git add -- src/ai_daily/selection.py tests/test_selection.py src/ai_daily/config.py tests/test_config.py
git commit -m "feat: add tiered digest candidate selection"
```

---

### Task 2: Per-run analyzer item limit

**Files:**
- Modify: `src/ai_daily/analyzer.py`
- Modify: `tests/test_analyzer.py`

**Interfaces:**
- Consumes: candidate evidence plus an optional `max_items` limit.
- Produces: `Analyzer.analyze(candidates, max_items=None)` that both prompts for and validates the effective limit.

- [ ] **Step 1: Write failing analyzer-limit test**

Add a test that supplies four candidate URLs, asks for a limit of 3, and verifies both the prompt and validation:

```python
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
```

- [ ] **Step 2: Run the analyzer test and verify RED**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_analyzer.py::test_analyze_prompts_for_and_enforces_per_run_maximum -q
```

Expected: failure because `Analyzer.analyze` does not accept `max_items`.

- [ ] **Step 3: Implement the effective limit**

Change the analyzer API and request construction:

```python
async def analyze(
    self,
    candidates: list[Candidate],
    max_items: int | None = None,
) -> Digest:
    effective_max = self._settings.max_items if max_items is None else max_items
    if not 1 <= effective_max <= self._settings.max_items:
        raise ValueError("max_items must be within configured maximum")
    response = await self._request(candidates, effective_max)
```

Validate `len(digest.items) > effective_max`, pass the limit through `_request`, and change `_user_message` to append:

```python
f"本次最多选择 {max_items} 条。",
```

- [ ] **Step 4: Run all analyzer tests and verify GREEN**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_analyzer.py -q
```

Expected: all analyzer tests pass.

- [ ] **Step 5: Commit analyzer limit**

```powershell
git add -- src/ai_daily/analyzer.py tests/test_analyzer.py
git commit -m "feat: support review-specific item limits"
```

---

### Task 3: Digest presentation modes and fixed status notice

**Files:**
- Modify: `src/ai_daily/dingtalk.py`
- Modify: `tests/test_dingtalk.py`

**Interfaces:**
- Consumes: a digest, report title, optional introductory note, scope text, or an empty-status report date.
- Produces: presentation-aware `render_digest` and `render_status_notice` while preserving the existing defaults.

- [ ] **Step 1: Write failing presentation tests**

Import `render_status_notice` and add:

```python
def test_render_review_labels_old_content_explicitly() -> None:
    text = render_digest(
        _digest(item_count=1),
        date(2026, 7, 20),
        168,
        report_title="AI 近期技术回顾",
        intro="今日无新的合格动态，以下为近期值得回顾的技术内容。",
        scope_text="回顾范围：最近 7 天",
    )[0]

    assert "# AI 近期技术回顾｜2026-07-20" in text
    assert "今日无新的合格动态" in text
    assert "回顾范围：最近 7 天" in text


def test_render_status_notice_is_fixed_and_contains_no_source_claim() -> None:
    parts = render_status_notice(date(2026, 7, 20))

    assert parts == [
        "# AI 技术日报｜2026-07-20\n\n"
        "## 今日状态\n\n"
        "今日暂未获取到可靠的 AI 技术内容。为避免生成未经来源支持的信息，"
        "本次仅发送状态通知。"
    ]
```

- [ ] **Step 2: Run rendering tests and verify RED**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_dingtalk.py::test_render_review_labels_old_content_explicitly tests/test_dingtalk.py::test_render_status_notice_is_fixed_and_contains_no_source_claim -q
```

Expected: import/signature failures because presentation options and `render_status_notice` do not exist.

- [ ] **Step 3: Implement presentation options**

Extend `_heading` and `_compose_part` with `report_title`, `intro`, and `scope_text`. Extend `render_digest` with keyword-only defaults:

```python
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
```

Use `_markdown_text` for `report_title` and `intro`; use the default scope `信息范围：最近 {window_hours} 小时` when `scope_text` is absent. Add:

```python
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
```

- [ ] **Step 4: Run all DingTalk tests and verify GREEN**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_dingtalk.py -q
```

Expected: all DingTalk tests pass, including the unchanged default format tests.

- [ ] **Step 5: Commit presentation support**

```powershell
git add -- src/ai_daily/dingtalk.py tests/test_dingtalk.py
git commit -m "feat: render fallback digest modes"
```

---

### Task 4: Orchestrate guaranteed content fallback

**Files:**
- Modify: `src/ai_daily/app.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: `select_candidate_batch`, per-run analyzer limits, and the new render options.
- Produces: one DingTalk delivery per successful run, including a fixed notice when no candidate is available.

- [ ] **Step 1: Write failing app tests for extended, review, and notice paths**

Update the app test settings helper with `fallback_window_hours=168`. Add tests that assert:

```python
@pytest.mark.asyncio
async def test_extended_mode_uses_unseen_seven_day_content(
    tmp_path, monkeypatch
) -> None:
    run_settings = settings(tmp_path, dry_run=True)
    selected = candidate("https://example.com/extended", hours_old=72)
    observations = {}
    install_client_spy(monkeypatch)

    async def collect(*args, **kwargs):
        return [selected]

    class AnalyzerSpy:
        def __init__(self, client, configured_settings) -> None:
            pass

        async def analyze(self, candidates, max_items=None):
            observations["analysis"] = (candidates, max_items)
            return digest_for(selected)

    def render(digest, report_date, window_hours, **kwargs):
        observations["render"] = (window_hours, kwargs)
        return ["preview"]

    monkeypatch.setattr(app, "collect_candidates", collect)
    monkeypatch.setattr(app, "Analyzer", AnalyzerSpy)
    monkeypatch.setattr(app, "render_digest", render)

    result = await app.run_digest(
        run_settings,
        SourceConfig(github_repositories=["owner/repository"]),
        now=NOW,
    )

    assert result.status == "dry-run"
    assert observations["analysis"] == ([selected], 8)
    assert observations["render"] == (
        168,
        {
            "report_title": "AI 近期技术精选",
            "intro": None,
            "scope_text": "信息范围：最近 7 天",
        },
    )


@pytest.mark.asyncio
async def test_review_mode_reuses_sent_content_with_three_item_limit(
    tmp_path, monkeypatch
) -> None:
    run_settings = settings(tmp_path, dry_run=True)
    selected = candidate("https://example.com/review", hours_old=72)
    sent_state = SentState()
    sent_state.mark_sent([str(selected.url)], NOW_UTC - timedelta(hours=1))
    sent_state.save(run_settings.state_path)
    observations = {}
    install_client_spy(monkeypatch)

    async def collect(*args, **kwargs):
        return [selected]

    class AnalyzerSpy:
        def __init__(self, client, configured_settings) -> None:
            pass

        async def analyze(self, candidates, max_items=None):
            observations["analysis"] = (candidates, max_items)
            return digest_for(selected)

    def render(digest, report_date, window_hours, **kwargs):
        observations["render"] = (window_hours, kwargs)
        return ["preview"]

    monkeypatch.setattr(app, "collect_candidates", collect)
    monkeypatch.setattr(app, "Analyzer", AnalyzerSpy)
    monkeypatch.setattr(app, "render_digest", render)

    await app.run_digest(
        run_settings,
        SourceConfig(github_repositories=["owner/repository"]),
        now=NOW,
    )

    assert observations["analysis"] == ([selected], 3)
    assert observations["render"] == (
        168,
        {
            "report_title": "AI 近期技术回顾",
            "intro": "今日无新的合格动态，以下为近期值得回顾的技术内容。",
            "scope_text": "回顾范围：最近 7 天",
        },
    )


@pytest.mark.asyncio
async def test_notice_mode_sends_and_marks_delivery_without_url_state(
    tmp_path, monkeypatch
) -> None:
    run_settings = settings(tmp_path, enforce_daily_once=True)
    observations = {}
    install_client_spy(monkeypatch)

    async def collect(*args, **kwargs):
        return []

    def analyzer_must_not_be_constructed(*args, **kwargs):
        raise AssertionError("notice mode called the analyzer")

    class SenderSpy:
        def __init__(self, client, configured_settings) -> None:
            pass

        async def send(self, parts, title):
            observations["send"] = (list(parts), title)

    monkeypatch.setattr(app, "collect_candidates", collect)
    monkeypatch.setattr(app, "Analyzer", analyzer_must_not_be_constructed)
    monkeypatch.setattr(app, "render_status_notice", lambda report_date: ["notice"])
    monkeypatch.setattr(app, "DingTalkSender", SenderSpy)

    result = await app.run_digest(
        run_settings,
        SourceConfig(github_repositories=["owner/repository"]),
        now=NOW,
    )

    assert result == app.RunResult("sent", 0, 0, 1)
    assert observations["send"] == (["notice"], "AI 技术日报｜2026-07-18")
    assert not run_settings.state_path.exists()
    assert DeliveryState.load(
        run_settings.delivery_state_path
    ).is_delivered(NOW.date())
```

Also change the normal-run collection assertion to use `NOW_UTC - timedelta(hours=168)`. Change every existing analyzer test double method signature to:

```python
async def analyze(self, candidates, max_items=None):
```

Change the old empty-run test expectation from `status="empty"` to `status="sent"`, `part_count=1`, a called sender, an absent URL state, and a present delivery date.

- [ ] **Step 2: Run app tests and verify RED**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_app.py -q
```

Expected: failures because the app still collects only 36 hours and exits with `status=empty`.

- [ ] **Step 3: Implement fallback orchestration**

Import `select_candidate_batch` and `render_status_notice`. Collect with:

```python
collection_cutoff = run_at - timedelta(hours=settings.fallback_window_hours)
```

After collection, select a batch:

```python
batch = select_candidate_batch(
    collected,
    now=run_at,
    sent_state=sent_state,
    primary_window_hours=settings.window_hours,
    fallback_window_hours=settings.fallback_window_hours,
    max_items=settings.max_items,
)
logger.info("prepared=%d", len(batch.candidates))
logger.info("mode=%s", batch.mode)
```

For `notice`, render fixed parts and skip the analyzer. For content modes, call:

```python
digest = await Analyzer(client, settings).analyze(
    batch.candidates,
    max_items=batch.max_items,
)
```

Use these presentation values:

```python
fresh: ("AI 技术日报", None, None)
extended: ("AI 近期技术精选", None, "信息范围：最近 7 天")
review: (
    "AI 近期技术回顾",
    "今日无新的合格动态，以下为近期值得回顾的技术内容。",
    "回顾范围：最近 7 天",
)
```

Generalize dry-run printing and DingTalk sending so both content and notice parts use the same path. Save URL state only when the model selected URLs; always save `DeliveryState` after a successful protected run. Log `status=sent` for the notice path.

- [ ] **Step 4: Run app and related tests and verify GREEN**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_app.py tests/test_selection.py tests/test_analyzer.py tests/test_dingtalk.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit orchestration**

```powershell
git add -- src/ai_daily/app.py tests/test_app.py
git commit -m "feat: always deliver a daily digest result"
```

---

### Task 5: Single schedule, Chinese documentation, and deployment

**Files:**
- Modify: `.github/workflows/daily.yml`
- Modify: `tests/test_workflows.py`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-20-daily-delivery-reliability-design.md`

**Interfaces:**
- Consumes: completed fallback behavior.
- Produces: one 08:30 schedule and accurate Chinese operations documentation.

- [ ] **Step 1: Write failing single-cron test**

Change the workflow expectation to:

```python
assert triggers["schedule"] == [{"cron": "30 0 * * *"}]
```

- [ ] **Step 2: Run the workflow test and verify RED**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_workflows.py::test_daily_workflow_has_schedule_and_safe_manual_default -q
```

Expected: failure because the workflow still has 08:40 and 08:50 entries.

- [ ] **Step 3: Remove compensation crons**

Change `.github/workflows/daily.yml` to:

```yaml
schedule:
  - cron: "30 0 * * *"
```

- [ ] **Step 4: Run workflow tests and verify GREEN**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_workflows.py -q
```

Expected: all workflow tests pass.

- [ ] **Step 5: Update Chinese documentation**

Make these exact documentation changes:

- Replace the README introduction's three-trigger sentence with: `项目可部署在 GitHub 私有仓库中，通过 GitHub Actions 每天北京时间 08:30 自动运行。`
- Add environment row: ``| `FALLBACK_WINDOW_HOURS` | 否 | `168` | 不小于 `WINDOW_HOURS` 的正整数；新内容不足时扩展候选范围，默认最近 7 天。 |``
- Replace the `empty` CLI row with: ``| `sent` | 钉钉已接受全部消息分片，包括固定状态通知 | 顺序发送 | 内容消息保存入选 URL；启用每日保护时记录当天成功 |`` while keeping only one `sent` row.
- Replace the source-preparation list with the four ordered modes: 36-hour unseen `fresh`, 168-hour unseen `extended`, 168-hour quality-filtered replay `review` capped at 3, and fixed `notice` without an AI call.
- State that scheduled and manual runs use `.state/sent.json` and `.state/manual/sent.json` respectively, while only scheduled runs mark `.state/deliveries.json`.
- Replace all three-cron guidance with the single UTC cron `30 0 * * *` and explicitly note that GitHub may delay it.
- Replace the `status=empty` troubleshooting section with a `今日状态` notice section explaining that a successful no-content run still sends and marks the date complete.
- Add this note at the top of `docs/superpowers/specs/2026-07-20-daily-delivery-reliability-design.md`: `> 后续设计《每日内容兜底推送设计》已取代本文的三个 cron 与空结果重试策略；当前仅保留北京时间 08:30 单次触发，并通过内容兜底保证成功运行有消息。`

- [ ] **Step 6: Run full verification and security checks**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest -q
git diff --check
rg -n "40 0 \* \* \*|50 0 \* \* \*|08:40|08:50|status=empty" .github/workflows/daily.yml tests/test_workflows.py README.md
```

Expected: the full suite passes, diff check succeeds, and the obsolete behavior scan returns no matches.

Scan tracked content for credential-shaped values without printing any credential supplied by the user.

- [ ] **Step 7: Commit schedule and documentation**

```powershell
git add -- .github/workflows/daily.yml tests/test_workflows.py README.md docs/superpowers/specs/2026-07-20-daily-delivery-reliability-design.md
git commit -m "docs: explain daily fallback delivery"
```

- [ ] **Step 8: Push and verify GitHub**

```powershell
git push origin master
```

Confirm through the GitHub API that the deployed workflow is active, contains only `30 0 * * *`, and the push-triggered Test workflow for the final head SHA concludes `success`. Do not trigger a real DingTalk message without separate approval.
