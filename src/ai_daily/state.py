import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ai_daily.filtering import canonicalize_url


_SHA256_KEY = re.compile(r"[0-9a-f]{64}")
_EVENT_TERM = re.compile(r"[0-9a-f]{16}")
_EVENT_TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]*|[\u4e00-\u9fff]+")
_SUBSTANTIVE_MARKERS = {
    "adds",
    "available",
    "benchmark",
    "license",
    "pricing",
    "supports",
    "下线",
    "价格",
    "升级",
    "开放",
    "新增",
    "许可",
    "评测",
    "支持",
    "性能",
}


@dataclass(frozen=True)
class EventRecord:
    identity_terms: frozenset[str]
    fact_markers: frozenset[str]
    reported_at: datetime


@dataclass
class SentState:
    entries: dict[str, datetime] = field(default_factory=dict)
    events: list[EventRecord] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "SentState":
        state_path = Path(path)
        if not state_path.exists():
            return cls()

        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("state must be a JSON object")
            events: list[EventRecord] = []
            if payload.get("version") == 2:
                entries_payload = payload.get("urls")
                events = _parse_events(payload.get("events"))
            else:
                entries_payload = payload
            if not isinstance(entries_payload, dict):
                raise TypeError("URL state must be a JSON object")
            entries = {}
            for key, value in entries_payload.items():
                if not isinstance(key, str) or _SHA256_KEY.fullmatch(key) is None:
                    raise ValueError("state key must be a SHA-256 digest")
                timestamp = datetime.fromisoformat(value)
                _require_aware(timestamp)
                entries[key] = timestamp
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("sent state file is malformed") from exc
        return cls(entries=entries, events=events)

    def is_sent(self, url: str) -> bool:
        return _url_hash(url) in self.entries

    def was_sent_since(self, url: str, cutoff: datetime) -> bool:
        _require_aware(cutoff)
        sent_at = self.entries.get(_url_hash(url))
        return sent_at is not None and sent_at >= cutoff

    def record_event(
        self,
        title: str,
        summary: str,
        reported_at: datetime,
    ) -> None:
        _require_aware(reported_at)
        terms, markers = _event_signature(title, summary)
        self.events.append(EventRecord(terms, markers, reported_at))
        cutoff = reported_at - timedelta(days=7)
        self.events = [
            event for event in self.events if event.reported_at >= cutoff
        ]

    def is_repeated_event(
        self,
        title: str,
        summary: str,
        observed_at: datetime,
    ) -> bool:
        _require_aware(observed_at)
        terms, markers = _event_signature(title, summary)
        cutoff = observed_at - timedelta(days=7)
        for event in self.events:
            if not cutoff <= event.reported_at <= observed_at:
                continue
            overlap = len(terms & event.identity_terms)
            union = len(terms | event.identity_terms)
            if union and overlap / union >= 0.7:
                if markers <= event.fact_markers:
                    return True
        return False

    def mark_sent(self, urls: Iterable[str], sent_at: datetime) -> None:
        _require_aware(sent_at)
        for url in urls:
            self.entries[_url_hash(url)] = sent_at

        cutoff = sent_at - timedelta(days=30)
        self.entries = {
            key: timestamp
            for key, timestamp in self.entries.items()
            if timestamp >= cutoff
        }

    def save(self, path: str | Path) -> None:
        state_path = Path(path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = state_path.with_suffix(state_path.suffix + ".tmp")
        url_payload = {
            key: timestamp.isoformat()
            for key, timestamp in sorted(self.entries.items())
        }
        payload = (
            {
                "version": 2,
                "urls": url_payload,
                "events": _event_payload(self.events),
            }
            if self.events
            else url_payload
        )
        try:
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, state_path)
        finally:
            temporary_path.unlink(missing_ok=True)


def _url_hash(url: str) -> str:
    canonical_url = canonicalize_url(url)
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


def _require_aware(timestamp: datetime) -> None:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")


def _parse_events(raw_events: object) -> list[EventRecord]:
    if not isinstance(raw_events, list):
        raise TypeError("events must be a list")
    events: list[EventRecord] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            raise TypeError("event must be an object")
        raw_terms = raw_event.get("identity_terms")
        raw_markers = raw_event.get("fact_markers")
        if (
            not isinstance(raw_terms, list)
            or not raw_terms
            or any(
                not isinstance(term, str)
                or _EVENT_TERM.fullmatch(term) is None
                for term in raw_terms
            )
            or not isinstance(raw_markers, list)
            or any(not isinstance(marker, str) for marker in raw_markers)
        ):
            raise ValueError("event signature is invalid")
        raw_reported_at = raw_event.get("reported_at")
        if not isinstance(raw_reported_at, str):
            raise ValueError("event timestamp is invalid")
        reported_at = datetime.fromisoformat(raw_reported_at)
        _require_aware(reported_at)
        events.append(
            EventRecord(
                identity_terms=frozenset(raw_terms),
                fact_markers=frozenset(raw_markers),
                reported_at=reported_at,
            )
        )
    return events


def _event_payload(events: list[EventRecord]) -> list[dict[str, object]]:
    return [
        {
            "identity_terms": sorted(event.identity_terms),
            "fact_markers": sorted(event.fact_markers),
            "reported_at": event.reported_at.isoformat(),
        }
        for event in events
    ]


def _event_signature(
    title: str,
    summary: str,
) -> tuple[frozenset[str], frozenset[str]]:
    normalized_title = title.casefold()
    terms: set[str] = set()
    for token in _EVENT_TOKEN.findall(normalized_title):
        if "\u4e00" <= token[0] <= "\u9fff":
            terms.update(
                token[index : index + 2]
                for index in range(max(1, len(token) - 1))
            )
        elif len(token) > 1:
            terms.add(token)
    hashed_terms = frozenset(
        hashlib.sha256(term.encode("utf-8")).hexdigest()[:16]
        for term in terms
    )
    fact_text = f"{title} {summary}".casefold()
    markers = {
        marker for marker in _SUBSTANTIVE_MARKERS if marker in fact_text
    }
    markers.update(re.findall(r"\bv?\d+(?:\.\d+)+\b", fact_text))
    return hashed_terms, frozenset(markers)
