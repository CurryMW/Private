# DingTalk AI Daily Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a private GitHub repository that publishes a Chinese AI technology digest to DingTalk every day at 08:30 Asia/Shanghai.

**Architecture:** A Python 3.12 command-line application gathers official RSS/Atom feeds, arXiv entries, Hugging Face daily papers, and selected GitHub releases. Focused modules normalize and filter candidates, call an OpenAI-compatible Claude endpoint for structured analysis, render DingTalk Markdown, send it through a custom robot webhook, and persist sent URL hashes through GitHub Actions Cache.

**Tech Stack:** Python 3.12, httpx, feedparser, huggingface-hub, Pydantic 2, PyYAML, pytest, GitHub Actions, DingTalk custom robot webhook.

## Global Constraints

- Run every day, including weekends, at Beijing time 08:30 using cron `30 0 * * *`.
- Use a private GitHub repository.
- Produce Chinese output with at most 8 high-value items from the preceding 36 hours.
- Cover only technology, models, open-source tools, papers, AI paradigms, and R&D trends; exclude financing, stock prices, personnel changes, and generic marketing.
- Use OpenAI Compatible Base URL `https://apiclaude.cc/v1` and model `claude-sonnet-4-6`.
- The DingTalk robot is unsigned; accept a full webhook or a base webhook plus access token.
- Keep `AI_API_KEY`, `DINGTALK_WEBHOOK`, and `DINGTALK_ACCESS_TOKEN` in GitHub Secrets only.
- Every selected item must preserve a candidate source URL; model output may not introduce a URL outside the candidate set.
- If fewer than 8 high-quality items exist, send fewer; if none exist, exit successfully without sending.
- `DRY_RUN` prints a redacted preview, sends no DingTalk request, and writes no sent state.

## File Map

- `pyproject.toml`: package metadata, controlled dependency ranges, pytest configuration, and CLI entry point.
- `.python-version`: fixes local and CI Python to 3.12.
- `.gitignore`: excludes virtual environments, caches, local secrets, and runtime state.
- `.env.example`: documents variable names without values.
- `config/sources.yaml`: maintainable list of RSS feeds, arXiv categories, Hugging Face settings, and GitHub repositories.
- `src/ai_daily/models.py`: shared Pydantic domain models.
- `src/ai_daily/config.py`: settings and source configuration loading/validation.
- `src/ai_daily/filtering.py`: URL canonicalization, relevance filtering, and duplicate removal.
- `src/ai_daily/state.py`: sent URL hash storage and retention.
- `src/ai_daily/sources.py`: all external source adapters and bounded-concurrency collection.
- `src/ai_daily/analyzer.py`: Claude prompt, OpenAI-compatible request, JSON parsing, and evidence validation.
- `src/ai_daily/dingtalk.py`: Markdown rendering, message splitting, webhook construction, and sending.
- `src/ai_daily/app.py`: end-to-end orchestration and dry-run behavior.
- `src/ai_daily/cli.py`: executable entry point and exit codes.
- `tests/`: unit and orchestration tests with no real network access.
- `.github/workflows/test.yml`: test workflow for pushes and pull requests.
- `.github/workflows/daily.yml`: scheduled/manual digest workflow and sent-state cache.
- `README.md`: setup, GitHub Secrets, dry run, live verification, source maintenance, and troubleshooting.

---

### Task 1: Bootstrap the package, domain models, and validated settings

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `src/ai_daily/__init__.py`
- Create: `src/ai_daily/models.py`
- Create: `src/ai_daily/config.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `Candidate`, `DigestItem`, `Digest`, `Settings`, and `load_settings()`.
- `Settings` exposes `ai_api_key: SecretStr`, `ai_base_url: str`, `ai_model: str`, `dingtalk_webhook: SecretStr`, `dingtalk_access_token: SecretStr | None`, `window_hours: int`, `max_items: int`, `timezone: str`, `dry_run: bool`, `state_path: Path`, and `github_token: SecretStr | None`.

- [ ] **Step 1: Write failing settings tests**

Create `tests/test_config.py` with exact cases for defaults, a complete DingTalk webhook, a base webhook plus token, and missing credentials:

```python
from urllib.parse import parse_qs, urlparse

import pytest

from ai_daily.config import load_settings


BASE_ENV = {
    "AI_API_KEY": "sk-test",
    "DINGTALK_WEBHOOK": "https://oapi.dingtalk.com/robot/send?access_token=test-token",
}


def test_load_settings_uses_approved_defaults() -> None:
    settings = load_settings(BASE_ENV)
    assert settings.ai_base_url == "https://apiclaude.cc/v1"
    assert settings.ai_model == "claude-sonnet-4-6"
    assert settings.window_hours == 36
    assert settings.max_items == 8
    assert settings.timezone == "Asia/Shanghai"
    assert settings.dry_run is False


def test_base_webhook_requires_access_token() -> None:
    env = BASE_ENV | {"DINGTALK_WEBHOOK": "https://oapi.dingtalk.com/robot/send"}
    with pytest.raises(ValueError, match="DINGTALK_ACCESS_TOKEN"):
        load_settings(env)


def test_base_webhook_accepts_separate_access_token() -> None:
    env = BASE_ENV | {
        "DINGTALK_WEBHOOK": "https://oapi.dingtalk.com/robot/send",
        "DINGTALK_ACCESS_TOKEN": "separate-token",
        "DRY_RUN": "true",
    }
    settings = load_settings(env)
    assert settings.dingtalk_access_token.get_secret_value() == "separate-token"
    assert settings.dry_run is True
    assert parse_qs(urlparse(settings.dingtalk_webhook.get_secret_value()).query) == {}


def test_missing_ai_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="AI_API_KEY"):
        load_settings({"DINGTALK_WEBHOOK": BASE_ENV["DINGTALK_WEBHOOK"]})
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `python -m pytest tests/test_config.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'ai_daily'`.

- [ ] **Step 3: Add package metadata and safe local configuration files**

Create `pyproject.toml` with Python `>=3.12,<3.13`, CLI entry point `ai-daily = "ai_daily.cli:main"`, and controlled dependency ranges:

```toml
[build-system]
requires = ["setuptools>=75,<82"]
build-backend = "setuptools.build_meta"

[project]
name = "dingtalk-ai-daily"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "feedparser>=6.0.12,<7",
  "httpx>=0.28.1,<1",
  "huggingface-hub>=1.23,<2",
  "pydantic>=2.13.4,<3",
  "python-dotenv>=1.1,<2",
  "PyYAML>=6.0,<7",
]

[project.optional-dependencies]
dev = ["pytest>=8.4,<10", "pytest-asyncio>=1.0,<2", "respx>=0.22,<1"]

[project.scripts]
ai-daily = "ai_daily.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
addopts = "-q"
asyncio_mode = "auto"
testpaths = ["tests"]
```

Create `.python-version` containing `3.12`, `.env.example` containing only variable names and non-secret defaults, and `.gitignore` containing `.env`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `*.pyc`, and `.state/`.

- [ ] **Step 4: Implement shared models and settings**

In `src/ai_daily/models.py`, implement:

```python
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class Category(StrEnum):
    MODEL = "模型发布"
    RESEARCH = "研究突破"
    OPEN_SOURCE = "开源工具"
    PARADIGM = "AI 范式"


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str = Field(min_length=12, max_length=64)
    title: str = Field(min_length=3, max_length=300)
    summary: str = Field(default="", max_length=6000)
    source: str = Field(min_length=2, max_length=100)
    url: HttpUrl
    published_at: datetime
    source_kind: str = Field(min_length=2, max_length=30)


class DigestItem(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    category: Category
    source: str = Field(min_length=2, max_length=100)
    summary: str = Field(min_length=10, max_length=600)
    impact: str = Field(min_length=10, max_length=600)
    url: HttpUrl


class Digest(BaseModel):
    overview: str = Field(min_length=10, max_length=800)
    items: list[DigestItem] = Field(min_length=1, max_length=8)
    trends: list[str] = Field(min_length=2, max_length=3)
```

In `src/ai_daily/config.py`, use `SecretStr` so settings representations cannot reveal secrets. Parse booleans from `1/true/yes/on`, validate that `DINGTALK_WEBHOOK` is HTTPS, and require `DINGTALK_ACCESS_TOKEN` only when the webhook query has no `access_token`. `load_settings(env: Mapping[str, str] | None = None) -> Settings` must load `.env` only when `env` is omitted and must never log the mapping.

- [ ] **Step 5: Install the package and run settings tests**

Run: `python -m pip install -e ".[dev]"`

Run: `python -m pytest tests/test_config.py -q`

Expected: `4 passed`.

- [ ] **Step 6: Commit the bootstrap**

```powershell
git add pyproject.toml .python-version .gitignore .env.example src/ai_daily tests/test_config.py
git commit -m "feat: bootstrap AI daily package"
```

---

### Task 2: Add canonicalization, relevance filtering, deduplication, and sent state

**Files:**
- Create: `src/ai_daily/filtering.py`
- Create: `src/ai_daily/state.py`
- Create: `tests/test_filtering.py`
- Create: `tests/test_state.py`

**Interfaces:**
- Consumes: `Candidate` from Task 1.
- Produces: `canonicalize_url(url: str) -> str`, `candidate_id(url: str) -> str`, `prepare_candidates(candidates, cutoff, sent_state) -> list[Candidate]`, and `SentState.load(path)`, `is_sent(url)`, `mark_sent(urls, sent_at)`, `save(path)`.

- [ ] **Step 1: Write failing filtering and state tests**

Cover these exact behaviors:

```python
from datetime import UTC, datetime, timedelta

from ai_daily.filtering import candidate_id, canonicalize_url, prepare_candidates
from ai_daily.models import Candidate
from ai_daily.state import SentState


NOW = datetime(2026, 7, 18, 0, 30, tzinfo=UTC)


def item(title: str, url: str, hours_old: int = 1, summary: str = "new model inference") -> Candidate:
    return Candidate(
        id=candidate_id(url), title=title, summary=summary, source="Official",
        url=url, published_at=NOW - timedelta(hours=hours_old), source_kind="rss",
    )


def test_canonicalize_url_removes_tracking_and_fragment() -> None:
    url = "https://example.com/post/?utm_source=x&ref=home#section"
    assert canonicalize_url(url) == "https://example.com/post"


def test_prepare_candidates_removes_old_business_duplicate_and_sent_items(tmp_path) -> None:
    sent = SentState()
    sent.mark_sent(["https://example.com/sent"], NOW)
    candidates = [
        item("New inference engine", "https://example.com/new"),
        item("New inference engine!", "https://example.com/duplicate"),
        item("Company financing round", "https://example.com/money", summary="funding valuation"),
        item("Old model paper", "https://example.com/old", hours_old=50),
        item("Already sent model", "https://example.com/sent"),
    ]
    result = prepare_candidates(candidates, NOW - timedelta(hours=36), sent)
    assert [str(candidate.url) for candidate in result] == ["https://example.com/new"]
```

State tests must also prove that JSON stores only SHA-256 URL hashes and ISO dates, prunes entries older than 30 days, and treats tracking variants of the same URL as sent.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `python -m pytest tests/test_filtering.py tests/test_state.py -q`

Expected: imports fail because `filtering.py` and `state.py` do not exist.

- [ ] **Step 3: Implement deterministic filtering**

Implement `canonicalize_url` using `urllib.parse`: lowercase scheme/host, remove fragments, remove known tracking keys (`utm_*`, `ref`, `source`, `campaign`), sort remaining query pairs, remove a trailing slash except for root, and reject non-HTTP(S) URLs. Implement `candidate_id` as the first 24 hexadecimal characters of SHA-256 over the canonical URL.

In `prepare_candidates`:

1. discard entries older than `cutoff` or without timezone-aware timestamps;
2. discard URLs already present in `SentState`;
3. discard an entry only when its combined title/summary contains a business keyword and no technical keyword;
4. deduplicate exact canonical URLs;
5. deduplicate normalized titles with `difflib.SequenceMatcher` ratio `>= 0.92`;
6. return newest-first candidates, capped later by the analyzer rather than here.

Use these business keywords: `funding`, `financing`, `valuation`, `stock`, `share price`, `earnings`, `appointment`, `融资`, `估值`, `股价`, `财报`, `任命`, `人事`. Use these technical keywords: `model`, `inference`, `training`, `benchmark`, `agent`, `paper`, `dataset`, `open source`, `模型`, `推理`, `训练`, `评测`, `智能体`, `论文`, `数据集`, `开源`.

- [ ] **Step 4: Implement privacy-preserving sent state**

`SentState` must hold `entries: dict[str, datetime]`, compute keys with the full SHA-256 of `canonicalize_url(url)`, tolerate a missing state file by returning an empty state, raise a clear `ValueError` for malformed JSON, write UTF-8 JSON atomically through a sibling `.tmp` file, and prune entries older than 30 days whenever `mark_sent` is called.

- [ ] **Step 5: Run focused and full tests**

Run: `python -m pytest tests/test_filtering.py tests/test_state.py -q`

Expected: all tests pass.

Run: `python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 6: Commit filtering and state**

```powershell
git add src/ai_daily/filtering.py src/ai_daily/state.py tests/test_filtering.py tests/test_state.py
git commit -m "feat: filter and track digest candidates"
```

---

### Task 3: Implement source configuration and source adapters

**Files:**
- Create: `config/sources.yaml`
- Modify: `src/ai_daily/config.py`
- Create: `src/ai_daily/sources.py`
- Create: `tests/fixtures/rss.xml`
- Create: `tests/fixtures/arxiv.xml`
- Create: `tests/test_sources.py`

**Interfaces:**
- Consumes: `Candidate` and `candidate_id`.
- Produces: `SourceConfig`, `load_source_config(path: Path) -> SourceConfig`, `fetch_rss`, `fetch_arxiv`, `fetch_huggingface_papers`, `fetch_github_releases`, and `collect_candidates(config, client, cutoff, now, github_token) -> list[Candidate]`.

- [ ] **Step 1: Add a concrete source list**

Create `config/sources.yaml`:

```yaml
rss:
  - name: OpenAI
    url: https://openai.com/news/rss.xml
  - name: Google DeepMind
    url: https://deepmind.google/blog/rss.xml
  - name: Microsoft Research
    url: https://www.microsoft.com/en-us/research/feed/
  - name: NVIDIA Developer Blog
    url: https://developer.nvidia.com/blog/feed/
  - name: Hugging Face Blog
    url: https://huggingface.co/blog/feed.xml
arxiv:
  categories: [cs.AI, cs.LG, cs.CL, cs.CV]
  max_results: 40
huggingface_daily_papers:
  enabled: true
  limit_per_day: 20
github_repositories:
  - huggingface/transformers
  - vllm-project/vllm
  - ggml-org/llama.cpp
  - langchain-ai/langchain
  - modelcontextprotocol/python-sdk
  - microsoft/semantic-kernel
  - crewAIInc/crewAI
  - langgenius/dify
```

These endpoints and repositories were selected from official project pages. GitHub releases use `GET /repos/{owner}/{repo}/releases`; Hugging Face daily papers use `HfApi.list_daily_papers(date=..., sort="trending", limit=..., token=False)`.

- [ ] **Step 2: Write adapter tests before implementation**

Use `respx` to mock HTTP and local fixture bytes for RSS/arXiv. Test that:

- RSS entries use the feed name as source and skip entries before `cutoff`.
- arXiv entries use `https://arxiv.org/abs/<id>` and source `arXiv`.
- GitHub ignores draft/prerelease releases, uses `published_at`, and sends `Accept: application/vnd.github+json` plus `X-GitHub-Api-Version: 2026-03-10`.
- Hugging Face calls both calendar dates intersecting a 36-hour window, maps paper IDs to `https://huggingface.co/papers/<id>`, and uses the requested date at 12:00 UTC if the SDK object has no timestamp.
- `collect_candidates` continues when one adapter raises, logs only the source label, and never logs a request URL containing query parameters.

- [ ] **Step 3: Run adapter tests and confirm they fail**

Run: `python -m pytest tests/test_sources.py -q`

Expected: import fails because `ai_daily.sources` is absent.

- [ ] **Step 4: Implement config models and parsing**

Add Pydantic models `RssSource(name: str, url: HttpUrl)`, `ArxivConfig(categories: list[str], max_results: int)`, `HuggingFaceConfig(enabled: bool, limit_per_day: int)`, and `SourceConfig`. `load_source_config` must call `yaml.safe_load`, reject unknown top-level keys, require at least one configured source, and return a validated `SourceConfig`.

- [ ] **Step 5: Implement adapters with bounded concurrency**

Implement `sources.py` with these rules:

- Every HTTP request has a 20-second timeout inherited from one shared `httpx.AsyncClient`.
- RSS, arXiv, and GitHub HTTP requests retry only timeout, connection, HTTP 429, and HTTP 5xx failures, at most 3 attempts with 1- and 2-second delays; parsing and validation failures are not retried.
- RSS and arXiv parsing uses `feedparser.parse(response.content)` and rejects `bozo` feeds only when they contain no usable entries.
- Dates are normalized to timezone-aware UTC using `published_parsed` or `updated_parsed`.
- GitHub requests at most 10 releases per repository and optionally adds `Authorization: Bearer <GITHUB_TOKEN>` without logging it.
- Hugging Face SDK calls run through `asyncio.to_thread` so they do not block the event loop.
- All candidates are created through one `_candidate(...)` helper that strips HTML, collapses whitespace, clips summaries at 6000 characters, and derives `id` with `candidate_id`.
- `collect_candidates` uses `asyncio.Semaphore(8)` and `asyncio.gather(..., return_exceptions=True)`; exceptions are logged as `source failed: <label>: <exception-class>` with no URL or response body.

- [ ] **Step 6: Run adapter and full tests**

Run: `python -m pytest tests/test_sources.py -q`

Expected: all adapter tests pass.

Run: `python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 7: Commit source adapters**

```powershell
git add config/sources.yaml src/ai_daily/config.py src/ai_daily/sources.py tests/fixtures tests/test_sources.py
git commit -m "feat: collect official AI source updates"
```

---

### Task 4: Implement evidence-bound Claude analysis

**Files:**
- Create: `src/ai_daily/analyzer.py`
- Create: `tests/test_analyzer.py`

**Interfaces:**
- Consumes: `Settings`, `Candidate`.
- Produces: `Analyzer(client: httpx.AsyncClient, settings: Settings)` and `await Analyzer.analyze(candidates: list[Candidate]) -> Digest`.

- [ ] **Step 1: Write failing analyzer tests**

Mock `POST https://apiclaude.cc/v1/chat/completions` and assert the request contains model `claude-sonnet-4-6`, temperature `0.2`, a Chinese system prompt, and candidate evidence. Return an OpenAI-compatible response whose `choices[0].message.content` is JSON and assert it becomes `Digest`.

Add negative tests that reject:

- Markdown-fenced JSON only after fences are stripped safely;
- a ninth item;
- a URL not present in the input candidates;
- an empty overview or fewer than two trends;
- HTTP 429 followed by success, proving no more than 3 attempts occur.

- [ ] **Step 2: Run analyzer tests and confirm failure**

Run: `python -m pytest tests/test_analyzer.py -q`

Expected: import fails because `ai_daily.analyzer` is absent.

- [ ] **Step 3: Implement the exact analysis contract**

Define a system prompt that says, in Chinese:

```text
你是严谨的 AI 技术编辑。只能使用候选材料中的事实，不得编造数字、日期、能力、评测结果或链接。
只选择技术、模型、研究、开源工具与 AI 范式内容，排除融资、股价、人事和营销新闻。
最多选择 8 条；质量不足时可以少选。趋势判断必须与事实摘要分开。
仅返回一个 JSON 对象，不要使用 Markdown 代码块。
```

The user message must serialize candidates as JSON with only `id`, `title`, `summary`, `source`, `url`, and ISO `published_at`, followed by the `Digest.model_json_schema()` schema. POST to `{ai_base_url.rstrip('/')}/chat/completions` with a 60-second timeout, `temperature: 0.2`, and `max_tokens: 6000`.

Parse `choices[0].message.content`, strip one surrounding triple-backtick block if present, call `Digest.model_validate_json`, enforce `len(items) <= settings.max_items`, and compare every canonical result URL with the canonical candidate URL set. Raise `AnalysisError` with no response body included when validation fails.

Retry only HTTP 429, timeout, connection errors, and 5xx responses with delays of 1 and 2 seconds for at most 3 attempts. Do not retry evidence or schema failures.

- [ ] **Step 4: Run analyzer and full tests**

Run: `python -m pytest tests/test_analyzer.py -q`

Expected: all analyzer tests pass.

Run: `python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit analyzer**

```powershell
git add src/ai_daily/analyzer.py tests/test_analyzer.py
git commit -m "feat: analyze candidates with Claude"
```

---

### Task 5: Render and send DingTalk Markdown safely

**Files:**
- Create: `src/ai_daily/dingtalk.py`
- Create: `tests/test_dingtalk.py`

**Interfaces:**
- Consumes: `Digest`, `Settings`.
- Produces: `build_webhook(settings) -> str`, `render_digest(digest, report_date, window_hours, max_chars=18000) -> list[str]`, `DingTalkSender(client, settings)`, and `await DingTalkSender.send(parts, title) -> None`.

- [ ] **Step 1: Write failing formatting and sender tests**

Tests must assert:

- a full webhook keeps its existing `access_token` exactly once;
- a base webhook receives the separately configured token through URL query construction;
- rendered output contains `AI 技术日报｜2026-07-18`, all required labels, clickable original links, `趋势观察`, and `信息范围：最近 36 小时`;
- a low `max_chars` splits only between complete items and labels titles `(1/2)` and `(2/2)`;
- sender payload is exactly `{"msgtype":"markdown","markdown":{"title":...,"text":...}}`;
- response `{"errcode":0,"errmsg":"ok"}` succeeds;
- nonzero `errcode` raises `DingTalkError` without including the webhook or token;
- HTTP 429 followed by success retries, capped at 3 attempts.

- [ ] **Step 2: Run DingTalk tests and confirm failure**

Run: `python -m pytest tests/test_dingtalk.py -q`

Expected: import fails because `ai_daily.dingtalk` is absent.

- [ ] **Step 3: Implement Markdown rendering and splitting**

Render each item as one indivisible block:

```text
### N. <title>
> 【类别】<category>  
> 【来源】<source>

**发生了什么：** <summary>

**为什么重要：** <impact>

[查看原文](<url>)
```

The first part includes the overview; the last part includes trends and the 36-hour footer. When more than one part is needed, rebuild each part with `AI 技术日报｜<date>（i/n）`. Raise `ValueError` if one complete item block alone exceeds `max_chars`; Pydantic field limits should prevent this under the production limit.

- [ ] **Step 4: Implement unsigned webhook delivery**

Use `urllib.parse.urlsplit`, `parse_qsl`, `urlencode`, and `urlunsplit` to add `access_token` only if absent. `DingTalkSender` uses the shared `httpx.AsyncClient`, posts one part at a time with a 20-second timeout, validates HTTP status and JSON, and retries transient failures with the same 3-attempt policy as the analyzer. Error messages may contain DingTalk `errcode` and `errmsg`, but never a request URL, query string, or payload.

- [ ] **Step 5: Run DingTalk and full tests**

Run: `python -m pytest tests/test_dingtalk.py -q`

Expected: all DingTalk tests pass.

Run: `python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 6: Commit DingTalk output**

```powershell
git add src/ai_daily/dingtalk.py tests/test_dingtalk.py
git commit -m "feat: render and send DingTalk digest"
```

---

### Task 6: Orchestrate collection, analysis, dry run, delivery, and state updates

**Files:**
- Create: `src/ai_daily/app.py`
- Create: `src/ai_daily/cli.py`
- Create: `tests/test_app.py`

**Interfaces:**
- Consumes: every interface from Tasks 1–5.
- Produces: `RunResult(status: Literal["sent", "dry-run", "empty"], candidate_count: int, selected_count: int, part_count: int)`, `run_digest(settings, source_config, now=None) -> RunResult`, and CLI `main() -> int`.

- [ ] **Step 1: Write orchestration tests first**

Patch the collector, analyzer, renderer, sender, and state store. Prove these flows:

1. Normal run collects, filters, analyzes, sends every part, then marks and saves selected URLs.
2. `DRY_RUN=true` prints each rendered part, never constructs/sends `DingTalkSender`, and never marks/saves state.
3. No prepared candidates returns `RunResult(status="empty", selected_count=0, part_count=0)` without model or DingTalk calls.
4. Failure on the second DingTalk part does not save state.
5. Selected URLs, rather than every candidate URL, are recorded after a successful send.
6. CLI returns `0` for sent/dry-run/empty and `1` for configuration, analysis, or delivery errors, logging only the exception class and safe message.

- [ ] **Step 2: Run orchestration tests and confirm failure**

Run: `python -m pytest tests/test_app.py -q`

Expected: import fails because `ai_daily.app` is absent.

- [ ] **Step 3: Implement the pipeline**

`run_digest` must:

1. set `now` to current UTC when omitted and compute `cutoff = now - timedelta(hours=settings.window_hours)`;
2. load `SentState` from `settings.state_path`;
3. create one `httpx.AsyncClient` with `User-Agent: dingtalk-ai-daily/0.1`;
4. call `collect_candidates` and `prepare_candidates`;
5. return `empty` if no candidates remain;
6. call `Analyzer.analyze` and `render_digest` using the report date in `ZoneInfo(settings.timezone)`;
7. for dry-run, print each part under a non-secret `--- preview i/n ---` marker and return without state mutation;
8. otherwise create `DingTalkSender`, send all parts, mark only `digest.items` URLs, save state, and return `sent`.

Use module logger `ai_daily.app` for counts only: collected, prepared, selected, parts, and final status. Never log candidate summaries, model responses, settings representations, webhook URLs, or headers.

Define the result type explicitly:

```python
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RunResult:
    status: Literal["sent", "dry-run", "empty"]
    candidate_count: int
    selected_count: int
    part_count: int
```

- [ ] **Step 4: Implement the CLI**

`main()` loads `.env`, calls `load_settings()`, loads `config/sources.yaml`, runs `asyncio.run(run_digest(...))`, prints one final count summary, and returns an exit code. Add `if __name__ == "__main__": raise SystemExit(main())`.

- [ ] **Step 5: Run orchestration and full tests**

Run: `python -m pytest tests/test_app.py -q`

Expected: all orchestration tests pass.

Run: `python -m pytest -q`

Expected: the complete suite passes with no network access.

- [ ] **Step 6: Commit the executable application**

```powershell
git add src/ai_daily/app.py src/ai_daily/cli.py tests/test_app.py
git commit -m "feat: orchestrate the daily digest"
```

---

### Task 7: Add CI, scheduled delivery, cache persistence, and security checks

**Files:**
- Create: `.github/workflows/test.yml`
- Create: `.github/workflows/daily.yml`
- Create: `tests/test_workflows.py`

**Interfaces:**
- Consumes: CLI command `python -m ai_daily.cli`, `.state/sent.json`, and GitHub Secrets.
- Produces: push/PR test automation and daily/manual delivery automation.

- [ ] **Step 1: Write workflow structure tests**

Parse both YAML files with `yaml.safe_load` after normalizing YAML 1.1 key `True` back to `on`. Assert:

- `test.yml` triggers on push and pull request and runs `python -m pytest -q`.
- `daily.yml` has cron `30 0 * * *` and `workflow_dispatch.inputs.dry_run`.
- both workflows use `actions/checkout@v6` and `actions/setup-python@v6` with Python `3.12`.
- daily workflow declares `permissions: contents: read` and a concurrency group.
- daily workflow restores `.state` with `actions/cache/restore@v5` and saves it only after a successful non-dry run with `actions/cache/save@v5` using a unique `${{ github.run_id }}` key.
- daily workflow maps only secret expressions to `AI_API_KEY`, `DINGTALK_WEBHOOK`, and `DINGTALK_ACCESS_TOKEN`.

- [ ] **Step 2: Run workflow tests and confirm failure**

Run: `python -m pytest tests/test_workflows.py -q`

Expected: failure because workflow files are absent.

- [ ] **Step 3: Create the test workflow**

Use this structure:

```yaml
name: Test
on:
  push:
  pull_request:
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
          cache: pip
      - run: python -m pip install -e ".[dev]"
      - run: python -m pytest -q
```

- [ ] **Step 4: Create the daily workflow**

Use `on.schedule.cron: "30 0 * * *"`, boolean `workflow_dispatch.inputs.dry_run` defaulting to `true`, `concurrency.group: dingtalk-ai-daily`, and `cancel-in-progress: false`. Restore state with key `dingtalk-ai-state-${{ runner.os }}-${{ github.run_id }}` and restore prefix `dingtalk-ai-state-${{ runner.os }}-`. Run the CLI with:

```yaml
env:
  AI_API_KEY: ${{ secrets.AI_API_KEY }}
  AI_BASE_URL: https://apiclaude.cc/v1
  AI_MODEL: claude-sonnet-4-6
  DINGTALK_WEBHOOK: ${{ secrets.DINGTALK_WEBHOOK }}
  DINGTALK_ACCESS_TOKEN: ${{ secrets.DINGTALK_ACCESS_TOKEN }}
  GITHUB_TOKEN: ${{ github.token }}
  DRY_RUN: ${{ github.event_name == 'workflow_dispatch' && inputs.dry_run || 'false' }}
run: python -m ai_daily.cli
```

Set the shown environment mapping at job level so every step sees the same `DRY_RUN` value. Save `.state` only under `if: success() && !(github.event_name == 'workflow_dispatch' && inputs.dry_run)`. Scheduled workflows run from the default branch; no workflow receives write permissions.

- [ ] **Step 5: Run workflow and full tests**

Run: `python -m pytest tests/test_workflows.py -q`

Expected: all workflow tests pass.

Run: `python -m pytest -q`

Expected: complete suite passes.

- [ ] **Step 6: Commit automation**

```powershell
git add .github/workflows tests/test_workflows.py
git commit -m "ci: schedule the daily DingTalk digest"
```

---

### Task 8: Document operation, perform security verification, and publish privately

**Files:**
- Create: `README.md`
- Modify: `.env.example`

**Interfaces:**
- Consumes: GitHub CLI authentication, workflow names, and secret names from earlier tasks.
- Produces: operator documentation and the private `dingtalk-ai-daily` GitHub repository.

- [ ] **Step 1: Write complete operator documentation**

README must include:

- architecture and content scope;
- local setup with Python 3.12 and `python -m pip install -e ".[dev]"`;
- exact variable names and explanation that a full DingTalk webhook makes `DINGTALK_ACCESS_TOKEN` optional;
- local `DRY_RUN=true` command examples for PowerShell;
- GitHub private repository creation;
- secure secret entry using interactive `gh secret set` commands, never command-line secret values;
- manual dry run, live run, schedule, cache behavior, and expected logs;
- source maintenance in `config/sources.yaml`;
- troubleshooting for invalid model JSON, HTTP 401/429/5xx, DingTalk nonzero `errcode`, empty digests, and delayed GitHub cron;
- warning that the third-party model endpoint receives candidate titles/summaries/URLs and should be used only if its data policy is acceptable.

- [ ] **Step 2: Run the complete offline verification suite**

Run: `python -m pytest -q`

Expected: all tests pass and no test contacts a real external service.

Run: `git grep -n -E "sk-[A-Za-z0-9]{12,}|access_token=[A-Za-z0-9_-]{12,}|AI_API_KEY=.+" -- ':!docs/superpowers/**' ':!.env.example'`

Expected: no matches.

Run: `git status --short`

Expected: only `README.md` and the intended `.env.example` update are uncommitted.

- [ ] **Step 3: Commit documentation**

```powershell
git add README.md .env.example
git commit -m "docs: add deployment and operations guide"
```

- [ ] **Step 4: Create and push the private GitHub repository**

Confirm authentication with `gh auth status`, then run:

```powershell
gh repo create dingtalk-ai-daily --private --source=. --remote=origin --push
```

Expected: GitHub reports a new private repository and `git status --short --branch` shows local `main` tracking `origin/main` with a clean tree.

- [ ] **Step 5: Add secrets through interactive prompts**

Run each command separately and paste the corresponding value only into the hidden prompt:

```powershell
gh secret set AI_API_KEY
gh secret set DINGTALK_WEBHOOK
gh secret set DINGTALK_ACCESS_TOKEN
```

Skip the third command only when `DINGTALK_WEBHOOK` is the complete URL containing `access_token`.

- [ ] **Step 6: Verify the deployed workflow in two stages**

First run a dry preview:

```powershell
gh workflow run daily.yml -f dry_run=true
gh run list --workflow daily.yml --limit 1
```

Capture and inspect the latest run without a manual identifier:

```powershell
$latestRunId = gh run list --workflow daily.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $latestRunId --exit-status
gh run view $latestRunId --log
```

Confirm a Chinese preview, zero secret values, and no DingTalk delivery.

Then run live delivery:

```powershell
gh workflow run daily.yml -f dry_run=false
$latestRunId = gh run list --workflow daily.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $latestRunId --exit-status
gh run view $latestRunId --log
```

Wait for completion, confirm the DingTalk group received all parts, and confirm the Actions summary reports status `sent`.

- [ ] **Step 7: Final acceptance check**

Verify all eight design acceptance criteria: offline tests pass; dry run is redacted; live DingTalk delivery succeeds; cron is `30 0 * * *`; at most 8 linked items appear; commercial news is excluded; a repeated run suppresses already sent URLs; and neither Git history nor Actions logs contain secrets.

## Verified implementation references

- DingTalk custom robot webhook: https://open.dingtalk.com/document/orgapp/custom-robot-access
- GitHub releases endpoint: https://docs.github.com/en/rest/releases/releases
- Hugging Face `list_daily_papers`: https://huggingface.co/docs/huggingface_hub/en/package_reference/hf_api
- GitHub checkout action: https://github.com/actions/checkout
- GitHub setup-python action: https://github.com/actions/setup-python
- GitHub cache action: https://github.com/actions/cache
