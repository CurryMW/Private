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
