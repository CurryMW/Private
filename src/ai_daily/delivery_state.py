import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path


@dataclass
class DeliveryState:
    entries: dict[date, datetime] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "DeliveryState":
        state_path = Path(path)
        if not state_path.exists():
            return cls()

        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("state must be a JSON object")
            entries: dict[date, datetime] = {}
            for key, value in payload.items():
                report_date = date.fromisoformat(key)
                if report_date.isoformat() != key:
                    raise ValueError("date must use ISO format")
                delivered_at = datetime.fromisoformat(value)
                _require_aware(delivered_at)
                entries[report_date] = delivered_at
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("delivery state file is malformed") from exc
        return cls(entries=entries)

    def is_delivered(self, report_date: date) -> bool:
        return report_date in self.entries

    def mark_delivered(self, report_date: date, delivered_at: datetime) -> None:
        _require_aware(delivered_at)
        self.entries[report_date] = delivered_at
        cutoff = report_date - timedelta(days=30)
        self.entries = {
            day: timestamp
            for day, timestamp in self.entries.items()
            if day >= cutoff
        }

    def save(self, path: str | Path) -> None:
        state_path = Path(path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = state_path.with_suffix(state_path.suffix + ".tmp")
        payload = {
            day.isoformat(): timestamp.isoformat()
            for day, timestamp in sorted(self.entries.items())
        }
        try:
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, state_path)
        finally:
            temporary_path.unlink(missing_ok=True)


def _require_aware(timestamp: datetime) -> None:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
