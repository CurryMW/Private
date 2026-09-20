import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

MAX_DAILY_SEARCH_REQUESTS = 20


@dataclass
class BaiduSearchUsage:
    report_date: date | None = None
    requests: int = 0

    def requests_for(self, report_date: date) -> int:
        return self.requests if self.report_date == report_date else 0

    def reserve(self, report_date: date) -> None:
        used = self.requests_for(report_date)
        if used >= MAX_DAILY_SEARCH_REQUESTS:
            raise ValueError("Baidu daily search quota exhausted")
        self.report_date = report_date
        self.requests = used + 1

    @classmethod
    def load(cls, path: str | Path) -> "BaiduSearchUsage":
        usage_path = Path(path)
        if not usage_path.exists():
            return cls()
        try:
            payload = json.loads(usage_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError
            raw_date = payload.get("date")
            requests = payload.get("requests")
            report_date = None if raw_date is None else date.fromisoformat(raw_date)
            if type(requests) is not int or not 0 <= requests <= MAX_DAILY_SEARCH_REQUESTS:
                raise ValueError
            return cls(report_date=report_date, requests=requests)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("Baidu usage state file is malformed") from exc

    def save(self, path: str | Path) -> None:
        usage_path = Path(path)
        usage_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = usage_path.with_suffix(usage_path.suffix + ".tmp")
        payload = {
            "date": self.report_date.isoformat() if self.report_date else None,
            "requests": self.requests,
        }
        try:
            temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary_path, usage_path)
        finally:
            temporary_path.unlink(missing_ok=True)


class BaiduSearchUsageFileStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> BaiduSearchUsage:
        return BaiduSearchUsage.load(self._path)

    def save(self, state: BaiduSearchUsage) -> None:
        state.save(self._path)
