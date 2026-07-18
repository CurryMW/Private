from datetime import UTC, datetime, timedelta

import pytest

from ai_daily.filtering import candidate_id, canonicalize_url, prepare_candidates
from ai_daily.models import Candidate
from ai_daily.state import SentState


NOW = datetime(2026, 7, 18, 0, 30, tzinfo=UTC)


def item(
    title: str,
    url: str,
    hours_old: int = 1,
    summary: str = "new model inference",
) -> Candidate:
    return Candidate(
        id=candidate_id(url),
        title=title,
        summary=summary,
        source="Official",
        url=url,
        published_at=NOW - timedelta(hours=hours_old),
        source_kind="rss",
    )


def test_canonicalize_url_removes_tracking_and_fragment() -> None:
    url = "https://example.com/post/?utm_source=x&ref=home#section"
    assert canonicalize_url(url) == "https://example.com/post"


def test_canonicalize_url_normalizes_and_sorts_query_parameters() -> None:
    url = "HTTPS://EXAMPLE.COM/path/?z=2&a=1&source=newsletter"
    assert canonicalize_url(url) == "https://example.com/path?a=1&z=2"


def test_canonicalize_url_normalizes_root_slash() -> None:
    assert canonicalize_url("https://example.com") == canonicalize_url(
        "https://example.com/"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com:443/model", "https://example.com/model"),
        ("http://example.com:80/model", "http://example.com/model"),
        ("https://example.com:8443/model", "https://example.com:8443/model"),
    ],
)
def test_canonicalize_url_strips_only_default_ports(url: str, expected: str) -> None:
    assert canonicalize_url(url) == expected


@pytest.mark.parametrize("url", ["ftp://example.com/file", "mailto:ai@example.com"])
def test_canonicalize_url_rejects_non_http_urls(url: str) -> None:
    with pytest.raises(ValueError, match="HTTP"):
        canonicalize_url(url)


def test_candidate_id_is_stable_across_tracking_variants() -> None:
    plain = candidate_id("https://example.com/post")
    tracked = candidate_id("https://EXAMPLE.com/post/?utm_medium=email#top")
    assert plain == tracked
    assert len(plain) == 24
    assert all(character in "0123456789abcdef" for character in plain)


def test_prepare_candidates_removes_old_business_duplicate_and_sent_items() -> None:
    sent = SentState()
    sent.mark_sent(["https://example.com/sent"], NOW)
    candidates = [
        item("New inference engine", "https://example.com/new"),
        item("New inference engine!", "https://example.com/duplicate"),
        item(
            "Company financing round",
            "https://example.com/money",
            summary="funding valuation",
        ),
        item("Old model paper", "https://example.com/old", hours_old=50),
        item("Already sent model", "https://example.com/sent"),
    ]
    result = prepare_candidates(candidates, NOW - timedelta(hours=36), sent)
    assert [str(candidate.url) for candidate in result] == ["https://example.com/new"]


def test_prepare_candidates_keeps_technical_business_item() -> None:
    candidate = item(
        "Company funding for open source model",
        "https://example.com/technical-funding",
        summary="funding supports model training",
    )
    assert prepare_candidates([candidate], NOW - timedelta(hours=36), SentState()) == [
        candidate
    ]


def test_prepare_candidates_keeps_newest_canonical_url_and_sorts_newest_first() -> None:
    older = item("Older model update", "https://example.com/older", hours_old=4)
    newer = item("Newer agent update", "https://example.com/newer", hours_old=1)
    tracked_duplicate = item(
        "Older model update details",
        "https://example.com/older/?utm_source=feed#details",
        hours_old=2,
    )
    result = prepare_candidates(
        [older, newer, tracked_duplicate], NOW - timedelta(hours=36), SentState()
    )
    assert result == [newer, tracked_duplicate]


def test_prepare_candidates_discards_naive_timestamps() -> None:
    candidate = item("Naive model update", "https://example.com/naive").model_copy(
        update={"published_at": datetime(2026, 7, 18, 0, 0)}
    )
    assert prepare_candidates(
        [candidate], NOW - timedelta(hours=36), SentState()
    ) == []


def test_prepare_candidates_keeps_newest_near_title_duplicate() -> None:
    older = item("New model inference engine", "https://example.com/older", hours_old=4)
    newer = item("New model inference engine!", "https://example.com/newer", hours_old=1)

    result = prepare_candidates(
        [older, newer], NOW - timedelta(hours=36), SentState()
    )

    assert result == [newer]
