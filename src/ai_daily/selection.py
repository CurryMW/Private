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
