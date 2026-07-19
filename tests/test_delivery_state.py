import json
from datetime import UTC, date, datetime, timedelta

import pytest

from ai_daily.delivery_state import DeliveryState


NOW = datetime(2026, 7, 20, 15, 35, tzinfo=UTC)
REPORT_DATE = date(2026, 7, 20)


def test_missing_file_is_empty(tmp_path) -> None:
    assert DeliveryState.load(tmp_path / "missing.json").entries == {}


def test_round_trip_records_iso_date_and_aware_time(tmp_path) -> None:
    path = tmp_path / "deliveries.json"
    state = DeliveryState()
    state.mark_delivered(REPORT_DATE, NOW)
    state.save(path)

    assert json.loads(path.read_text(encoding="utf-8")) == {
        REPORT_DATE.isoformat(): NOW.isoformat()
    }
    assert DeliveryState.load(path).is_delivered(REPORT_DATE)
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_mark_delivered_prunes_dates_older_than_thirty_days() -> None:
    old_date = REPORT_DATE - timedelta(days=31)
    recent_date = REPORT_DATE - timedelta(days=30)
    state = DeliveryState(
        entries={
            old_date: NOW - timedelta(days=31),
            recent_date: NOW - timedelta(days=30),
        }
    )

    state.mark_delivered(REPORT_DATE, NOW)

    assert not state.is_delivered(old_date)
    assert state.is_delivered(recent_date)
    assert state.is_delivered(REPORT_DATE)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"20-07-2026": NOW.isoformat()},
        {REPORT_DATE.isoformat(): "2026-07-20T15:35:00"},
    ],
)
def test_load_rejects_malformed_state(tmp_path, payload) -> None:
    path = tmp_path / "deliveries.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="delivery state"):
        DeliveryState.load(path)


def test_mark_delivered_rejects_naive_timestamp() -> None:
    state = DeliveryState()

    with pytest.raises(ValueError, match="timezone-aware"):
        state.mark_delivered(REPORT_DATE, datetime(2026, 7, 20, 15, 35))


def test_save_removes_temporary_file_when_replace_fails(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "deliveries.json"
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    state = DeliveryState()
    state.mark_delivered(REPORT_DATE, NOW)

    def fail_replace(source, destination) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("ai_daily.delivery_state.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failure"):
        state.save(path)

    assert not temporary_path.exists()
