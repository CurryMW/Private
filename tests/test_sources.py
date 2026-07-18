import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from pydantic import ValidationError

from ai_daily.config import (
    ArxivConfig,
    HuggingFaceConfig,
    RssSource,
    SourceConfig,
    load_source_config,
)
from ai_daily.filtering import candidate_id
from ai_daily.sources import (
    collect_candidates,
    fetch_arxiv,
    fetch_github_releases,
    fetch_huggingface_papers,
    fetch_rss,
)


NOW = datetime(2026, 7, 18, 0, 30, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=36)
FIXTURES = Path(__file__).parent / "fixtures"


def test_load_source_config_validates_the_concrete_shape(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(
        """
rss:
  - name: Official Feed
    url: https://news.example.com/feed.xml
arxiv:
  categories: [cs.AI]
  max_results: 40
huggingface_daily_papers:
  enabled: true
  limit_per_day: 20
github_repositories:
  - example/ai-project
""".strip(),
        encoding="utf-8",
    )

    config = load_source_config(path)

    assert config.rss[0].name == "Official Feed"
    assert str(config.rss[0].url) == "https://news.example.com/feed.xml"
    assert config.arxiv.categories == ["cs.AI"]
    assert config.huggingface_daily_papers.limit_per_day == 20
    assert config.github_repositories == ["example/ai-project"]


def test_load_source_config_rejects_unknown_top_level_keys(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text("rss: []\nunexpected: true\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="unexpected"):
        load_source_config(path)


def test_load_source_config_requires_one_enabled_source(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(
        "huggingface_daily_papers:\n  enabled: false\n  limit_per_day: 20\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="source"):
        load_source_config(path)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_rss_uses_feed_name_and_skips_entries_before_cutoff() -> None:
    source = RssSource(name="Official Lab", url="https://news.example.com/feed.xml")
    route = respx.get(str(source.url)).mock(
        return_value=httpx.Response(200, content=(FIXTURES / "rss.xml").read_bytes())
    )

    async with httpx.AsyncClient() as client:
        candidates = await fetch_rss(source, client, CUTOFF)

    assert route.called
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source == "Official Lab"
    assert candidate.source_kind == "rss"
    assert candidate.title == "New inference & training system"
    assert candidate.summary == "A faster inference system."
    assert candidate.published_at == datetime(2026, 7, 17, 23, 30, tzinfo=UTC)
    assert candidate.id == candidate_id(str(candidate.url))
    assert route.calls[0].request.extensions["timeout"]["read"] == 20.0


@pytest.mark.asyncio
@respx.mock
async def test_fetch_arxiv_uses_canonical_abs_url_and_source() -> None:
    route = respx.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())
    )
    config = ArxivConfig(categories=["cs.AI", "cs.LG"], max_results=40)

    async with httpx.AsyncClient() as client:
        candidates = await fetch_arxiv(config, client, CUTOFF)

    assert route.called
    request = route.calls[0].request
    assert request.url.params["search_query"] == "cat:cs.AI OR cat:cs.LG"
    assert request.url.params["max_results"] == "40"
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source == "arXiv"
    assert candidate.source_kind == "arxiv"
    assert str(candidate.url) == "https://arxiv.org/abs/2607.12345v1"
    assert candidate.summary == "We introduce agent reasoning with stronger evaluation."


@pytest.mark.asyncio
@respx.mock
async def test_fetch_github_ignores_nonfinal_releases_and_sets_api_headers() -> None:
    route = respx.get("https://api.github.com/repos/example/project/releases").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "v2.0",
                    "tag_name": "v2.0.0",
                    "body": "<p>New inference runtime.</p>",
                    "html_url": "https://github.com/example/project/releases/tag/v2.0.0",
                    "published_at": "2026-07-17T20:00:00Z",
                    "draft": False,
                    "prerelease": False,
                },
                {
                    "name": "v2.1-rc1",
                    "tag_name": "v2.1-rc1",
                    "body": "Release candidate",
                    "html_url": "https://github.com/example/project/releases/tag/v2.1-rc1",
                    "published_at": "2026-07-17T21:00:00Z",
                    "draft": False,
                    "prerelease": True,
                },
                {
                    "name": "Draft",
                    "tag_name": "draft",
                    "body": "Draft release",
                    "html_url": "https://github.com/example/project/releases/tag/draft",
                    "published_at": "2026-07-17T22:00:00Z",
                    "draft": True,
                    "prerelease": False,
                },
            ],
        )
    )

    async with httpx.AsyncClient() as client:
        candidates = await fetch_github_releases(
            "example/project", client, CUTOFF, github_token="github-secret"
        )

    request = route.calls[0].request
    assert request.url.params["per_page"] == "10"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
    assert request.headers["Authorization"] == "Bearer github-secret"
    assert [candidate.title for candidate in candidates] == ["v2.0"]
    assert candidates[0].published_at == datetime(2026, 7, 17, 20, tzinfo=UTC)
    assert candidates[0].source == "GitHub: example/project"
    assert candidates[0].source_kind == "github"


@pytest.mark.asyncio
async def test_fetch_huggingface_calls_each_intersecting_date_and_uses_noon_fallback(
    monkeypatch,
) -> None:
    calls: list[dict] = []

    class FakeApi:
        def list_daily_papers(self, **kwargs):
            calls.append(kwargs)
            paper_id = "2607.10001" if kwargs["date"] == "2026-07-17" else "2607.10002"
            return [
                SimpleNamespace(
                    id=paper_id,
                    title=f"Paper for {kwargs['date']}",
                    summary="<p>A daily paper summary.</p>",
                    published_at=None,
                    submitted_at=None,
                )
            ]

    monkeypatch.setattr("ai_daily.sources.HfApi", FakeApi)
    config = HuggingFaceConfig(enabled=True, limit_per_day=20)
    now = datetime(2026, 7, 18, 12, tzinfo=UTC)
    cutoff = now - timedelta(hours=36)

    candidates = await fetch_huggingface_papers(config, cutoff, now)

    assert calls == [
        {"date": "2026-07-17", "sort": "trending", "limit": 20, "token": False},
        {"date": "2026-07-18", "sort": "trending", "limit": 20, "token": False},
    ]
    assert [str(candidate.url) for candidate in candidates] == [
        "https://huggingface.co/papers/2607.10001",
        "https://huggingface.co/papers/2607.10002",
    ]
    assert [candidate.published_at for candidate in candidates] == [
        datetime(2026, 7, 17, 12, tzinfo=UTC),
        datetime(2026, 7, 18, 12, tzinfo=UTC),
    ]
    assert all(candidate.source == "Hugging Face Papers" for candidate in candidates)


@pytest.mark.asyncio
async def test_huggingface_keeps_current_date_paper_with_no_timestamp_at_midnight(
    monkeypatch,
) -> None:
    class FakeApi:
        def list_daily_papers(self, **kwargs):
            if kwargs["date"] != "2026-07-18":
                return []
            return [
                SimpleNamespace(
                    id="2607.10003",
                    title="Current date paper",
                    summary="A paper featured just after midnight.",
                    published_at=None,
                    submitted_at=None,
                )
            ]

    monkeypatch.setattr("ai_daily.sources.HfApi", FakeApi)
    config = HuggingFaceConfig(enabled=True, limit_per_day=20)

    candidates = await fetch_huggingface_papers(config, CUTOFF, NOW)

    assert len(candidates) == 1
    assert candidates[0].published_at == datetime(2026, 7, 18, 12, tzinfo=UTC)
    assert str(candidates[0].url) == "https://huggingface.co/papers/2607.10003"


@pytest.mark.asyncio
async def test_huggingface_prefers_daily_submission_time_over_original_publication(
    monkeypatch,
) -> None:
    featured_at = datetime(2026, 7, 17, 18, tzinfo=UTC)

    class FakeApi:
        def list_daily_papers(self, **kwargs):
            if kwargs["date"] != "2026-07-17":
                return []
            return [
                SimpleNamespace(
                    id="2501.00001",
                    title="Older paper featured today",
                    summary="An older paper newly submitted to the daily list.",
                    published_at=datetime(2025, 1, 1, tzinfo=UTC),
                    submitted_at=featured_at,
                )
            ]

    monkeypatch.setattr("ai_daily.sources.HfApi", FakeApi)
    config = HuggingFaceConfig(enabled=True, limit_per_day=20)

    candidates = await fetch_huggingface_papers(config, CUTOFF, NOW)

    assert len(candidates) == 1
    assert candidates[0].published_at == featured_at
    assert str(candidates[0].url) == "https://huggingface.co/papers/2501.00001"


@pytest.mark.asyncio
@respx.mock
async def test_http_retry_is_limited_to_three_transient_attempts(monkeypatch) -> None:
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ai_daily.sources.asyncio.sleep", fake_sleep)
    source = RssSource(name="Official Lab", url="https://news.example.com/feed.xml")
    route = respx.get(str(source.url)).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(503),
            httpx.Response(500),
        ]
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_rss(source, client, CUTOFF)

    assert route.call_count == 3
    assert sleeps == [1, 2]


@pytest.mark.asyncio
@respx.mock
async def test_http_retry_handles_connection_error_then_succeeds(monkeypatch) -> None:
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ai_daily.sources.asyncio.sleep", fake_sleep)
    source = RssSource(name="Official Lab", url="https://news.example.com/feed.xml")
    request = httpx.Request("GET", str(source.url))
    route = respx.get(str(source.url)).mock(
        side_effect=[
            httpx.ConnectError("offline", request=request),
            httpx.Response(200, content=(FIXTURES / "rss.xml").read_bytes()),
        ]
    )

    async with httpx.AsyncClient() as client:
        candidates = await fetch_rss(source, client, CUTOFF)

    assert len(candidates) == 1
    assert route.call_count == 2
    assert sleeps == [1]


@pytest.mark.asyncio
@respx.mock
async def test_http_retry_handles_timeout_then_succeeds(monkeypatch) -> None:
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ai_daily.sources.asyncio.sleep", fake_sleep)
    source = RssSource(name="Official Lab", url="https://news.example.com/feed.xml")
    request = httpx.Request("GET", str(source.url))
    route = respx.get(str(source.url)).mock(
        side_effect=[
            httpx.ReadTimeout("timed out", request=request),
            httpx.Response(200, content=(FIXTURES / "rss.xml").read_bytes()),
        ]
    )

    async with httpx.AsyncClient() as client:
        candidates = await fetch_rss(source, client, CUTOFF)

    assert len(candidates) == 1
    assert route.call_count == 2
    assert sleeps == [1]


@pytest.mark.asyncio
@respx.mock
async def test_http_retry_does_not_retry_nontransient_status(monkeypatch) -> None:
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ai_daily.sources.asyncio.sleep", fake_sleep)
    source = RssSource(name="Official Lab", url="https://news.example.com/feed.xml")
    route = respx.get(str(source.url)).mock(return_value=httpx.Response(404))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_rss(source, client, CUTOFF)

    assert route.call_count == 1
    assert sleeps == []


@pytest.mark.asyncio
@respx.mock
async def test_bozo_feed_with_usable_entry_is_accepted() -> None:
    malformed_with_entry = b"""<rss><channel><item>
<title>Usable model update</title>
<link>https://news.example.com/usable</link>
<description>New inference details.</description>
<pubDate>Fri, 17 Jul 2026 23:30:00 GMT</pubDate>
</item>"""
    source = RssSource(name="Official Lab", url="https://news.example.com/feed.xml")
    route = respx.get(str(source.url)).mock(
        return_value=httpx.Response(200, content=malformed_with_entry)
    )

    async with httpx.AsyncClient() as client:
        candidates = await fetch_rss(source, client, CUTOFF)

    assert [candidate.title for candidate in candidates] == ["Usable model update"]
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_bozo_feed_without_usable_entries_is_rejected_without_retry(
    monkeypatch,
) -> None:
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ai_daily.sources.asyncio.sleep", fake_sleep)
    source = RssSource(name="Official Lab", url="https://news.example.com/feed.xml")
    route = respx.get(str(source.url)).mock(
        return_value=httpx.Response(200, content=b"<rss><channel><broken>")
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="usable"):
            await fetch_rss(source, client, CUTOFF)

    assert route.call_count == 1
    assert sleeps == []


@pytest.mark.asyncio
@respx.mock
async def test_feed_entry_uses_updated_timestamp_when_published_is_absent() -> None:
    atom = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Updates</title>
  <entry>
    <title>Updated agent runtime</title>
    <link href="https://news.example.com/updated"/>
    <updated>2026-07-17T21:15:00Z</updated>
    <summary>Runtime improvements.</summary>
  </entry>
</feed>"""
    source = RssSource(name="Official Lab", url="https://news.example.com/feed.xml")
    respx.get(str(source.url)).mock(return_value=httpx.Response(200, content=atom))

    async with httpx.AsyncClient() as client:
        candidates = await fetch_rss(source, client, CUTOFF)

    assert candidates[0].published_at == datetime(2026, 7, 17, 21, 15, tzinfo=UTC)


@pytest.mark.asyncio
@respx.mock
async def test_candidate_validation_failure_is_not_retried(monkeypatch) -> None:
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ai_daily.sources.asyncio.sleep", fake_sleep)
    route = respx.get("https://api.github.com/repos/example/project/releases").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "x",
                    "tag_name": "x",
                    "body": "Valid body",
                    "html_url": "https://github.com/example/project/releases/tag/x",
                    "published_at": "2026-07-17T20:00:00Z",
                    "draft": False,
                    "prerelease": False,
                }
            ],
        )
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(ValidationError):
            await fetch_github_releases("example/project", client, CUTOFF)

    assert route.call_count == 1
    assert sleeps == []


@pytest.mark.asyncio
@respx.mock
async def test_github_omits_authorization_header_without_token() -> None:
    route = respx.get("https://api.github.com/repos/example/project/releases").mock(
        return_value=httpx.Response(200, json=[])
    )

    async with httpx.AsyncClient() as client:
        assert await fetch_github_releases("example/project", client, CUTOFF) == []

    assert "Authorization" not in route.calls[0].request.headers


@pytest.mark.parametrize(
    "payload",
    [
        {
            "rss": [
                {
                    "name": "Official",
                    "url": "https://news.example.com/feed.xml",
                    "unexpected": True,
                }
            ]
        },
        {
            "arxiv": {
                "categories": ["cs.AI"],
                "max_results": 10,
                "unexpected": True,
            }
        },
        {
            "huggingface_daily_papers": {
                "enabled": True,
                "limit_per_day": 10,
                "unexpected": True,
            }
        },
    ],
)
def test_source_config_rejects_unknown_nested_keys(payload) -> None:
    with pytest.raises(ValidationError, match="unexpected"):
        SourceConfig.model_validate(payload)


@pytest.mark.asyncio
async def test_collect_candidates_continues_after_failure_and_logs_safely(
    monkeypatch, caplog
) -> None:
    safe_candidate = SimpleNamespace(marker="safe")

    async def fail_rss(source, client, cutoff):
        raise RuntimeError("https://private.example/feed?access_token=secret-value")

    async def succeed_github(repository, client, cutoff, github_token=None):
        return [safe_candidate]

    monkeypatch.setattr("ai_daily.sources.fetch_rss", fail_rss)
    monkeypatch.setattr("ai_daily.sources.fetch_github_releases", succeed_github)
    config = SourceConfig(
        rss=[RssSource(name="Failing Feed", url="https://private.example/feed?key=secret")],
        github_repositories=["example/project"],
    )

    with caplog.at_level(logging.ERROR, logger="ai_daily.sources"):
        async with httpx.AsyncClient() as client:
            result = await collect_candidates(config, client, CUTOFF, NOW, "token")

    assert result == [safe_candidate]
    assert caplog.messages == ["source failed: Failing Feed: RuntimeError"]
    assert "private.example" not in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_collect_candidates_bounds_concurrency_at_eight(monkeypatch) -> None:
    active = 0
    maximum_active = 0
    release = asyncio.Event()

    async def blocked_rss(source, client, cutoff):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        if maximum_active == 8:
            release.set()
        await release.wait()
        await asyncio.sleep(0)
        active -= 1
        return []

    monkeypatch.setattr("ai_daily.sources.fetch_rss", blocked_rss)
    config = SourceConfig(
        rss=[
            RssSource(name=f"Feed {index}", url=f"https://feed{index}.example/rss")
            for index in range(12)
        ]
    )

    async with httpx.AsyncClient() as client:
        await asyncio.wait_for(
            collect_candidates(config, client, CUTOFF, NOW, None), timeout=1
        )

    assert maximum_active == 8
