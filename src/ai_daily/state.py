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


@dataclass
class SentState:
    entries: dict[str, datetime] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "SentState":
        state_path = Path(path)
        if not state_path.exists():
            return cls()

        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("state must be a JSON object")
            entries = {}
            for key, value in payload.items():
                if not isinstance(key, str) or _SHA256_KEY.fullmatch(key) is None:
                    raise ValueError("state key must be a SHA-256 digest")
                timestamp = datetime.fromisoformat(value)
                _require_aware(timestamp)
                entries[key] = timestamp
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("sent state file is malformed") from exc
        return cls(entries=entries)

    def is_sent(self, url: str) -> bool:
        return _url_hash(url) in self.entries

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
        payload = {
            key: timestamp.isoformat()
            for key, timestamp in sorted(self.entries.items())
        }
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
