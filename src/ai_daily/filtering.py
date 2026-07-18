import hashlib
import re
from collections.abc import Iterable
from datetime import datetime
from difflib import SequenceMatcher
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ai_daily.models import Candidate

if TYPE_CHECKING:
    from ai_daily.state import SentState


_TRACKING_KEYS = {"ref", "source", "campaign"}
_BUSINESS_KEYWORDS = {
    "funding",
    "financing",
    "valuation",
    "stock",
    "share price",
    "earnings",
    "appointment",
    "融资",
    "估值",
    "股价",
    "财报",
    "任命",
    "人事",
}
_TECHNICAL_KEYWORDS = {
    "model",
    "inference",
    "training",
    "benchmark",
    "agent",
    "paper",
    "dataset",
    "open source",
    "模型",
    "推理",
    "训练",
    "评测",
    "智能体",
    "论文",
    "数据集",
    "开源",
}


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("URL must use HTTP or HTTPS")
    if not parts.hostname:
        raise ValueError("HTTP URL must include a host")

    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    if parts.username is not None:
        credentials = parts.username
        if parts.password is not None:
            credentials = f"{credentials}:{parts.password}"
        host = f"{credentials}@{host}"

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_key(key)
    ]
    query_pairs.sort()
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, host, path, urlencode(query_pairs), ""))


def candidate_id(url: str) -> str:
    canonical_url = canonicalize_url(url)
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:24]


def prepare_candidates(
    candidates: Iterable[Candidate], cutoff: datetime, sent_state: "SentState"
) -> list[Candidate]:
    prepared: list[Candidate] = []
    canonical_urls: set[str] = set()
    normalized_titles: list[str] = []

    eligible_candidates = (
        candidate
        for candidate in candidates
        if _is_aware(candidate.published_at) and candidate.published_at >= cutoff
    )
    for candidate in sorted(
        eligible_candidates,
        key=lambda candidate: candidate.published_at,
        reverse=True,
    ):
        if sent_state.is_sent(str(candidate.url)):
            continue

        text = f"{candidate.title} {candidate.summary}".casefold()
        if _contains_keyword(text, _BUSINESS_KEYWORDS) and not _contains_keyword(
            text, _TECHNICAL_KEYWORDS
        ):
            continue

        canonical_url = canonicalize_url(str(candidate.url))
        if canonical_url in canonical_urls:
            continue

        normalized_title = _normalize_title(candidate.title)
        if any(
            SequenceMatcher(None, normalized_title, existing).ratio() >= 0.92
            for existing in normalized_titles
        ):
            continue

        prepared.append(candidate)
        canonical_urls.add(canonical_url)
        normalized_titles.append(normalized_title)

    return prepared


def _is_tracking_key(key: str) -> bool:
    normalized = key.casefold()
    return normalized.startswith("utm_") or normalized in _TRACKING_KEYS


def _contains_keyword(text: str, keywords: set[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _normalize_title(title: str) -> str:
    return "".join(re.findall(r"\w+", title.casefold(), flags=re.UNICODE))


def _is_aware(timestamp: datetime) -> bool:
    return timestamp.tzinfo is not None and timestamp.utcoffset() is not None
