import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from ai_daily.filtering import canonicalize_url
from ai_daily.state import SentState


NOW = datetime(2026, 7, 18, 0, 30, tzinfo=UTC)


def state_key(url: str) -> str:
    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()


def test_state_round_trip_stores_only_hashes_and_iso_dates(tmp_path) -> None:
    path = tmp_path / "sent.json"
    url = "https://example.com/model?utm_source=feed"
    state = SentState()
    state.mark_sent([url], NOW)

    state.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {state_key(url): NOW.isoformat()}
    assert "example.com" not in path.read_text(encoding="utf-8")
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    assert SentState.load(path).entries == state.entries


def test_tracking_variant_is_treated_as_sent() -> None:
    state = SentState()
    state.mark_sent(["https://example.com/model"], NOW)
    assert state.is_sent("https://example.com/model?utm_campaign=launch#results")


def test_mark_sent_prunes_entries_older_than_thirty_days() -> None:
    old_url = "https://example.com/old"
    recent_url = "https://example.com/recent"
    new_url = "https://example.com/new"
    state = SentState(
        entries={
            state_key(old_url): NOW - timedelta(days=31),
            state_key(recent_url): NOW - timedelta(days=30),
        }
    )

    state.mark_sent([new_url], NOW)

    assert not state.is_sent(old_url)
    assert state.is_sent(recent_url)
    assert state.is_sent(new_url)


def test_load_missing_file_returns_empty_state(tmp_path) -> None:
    assert SentState.load(tmp_path / "missing.json").entries == {}


def test_load_malformed_json_raises_clear_value_error(tmp_path) -> None:
    path = tmp_path / "sent.json"
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="state"):
        SentState.load(path)


@pytest.mark.parametrize(
    "invalid_key",
    [
        "https://example.com/raw-url",
        "A" * 64,
        "a" * 63,
        "g" * 64,
    ],
)
def test_load_rejects_non_sha256_keys(tmp_path, invalid_key: str) -> None:
    path = tmp_path / "sent.json"
    path.write_text(json.dumps({invalid_key: NOW.isoformat()}), encoding="utf-8")

    with pytest.raises(ValueError, match="state"):
        SentState.load(path)


def test_load_rejects_timezone_naive_timestamp(tmp_path) -> None:
    path = tmp_path / "sent.json"
    path.write_text(
        json.dumps({state_key("https://example.com/model"): "2026-07-18T00:30:00"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="state"):
        SentState.load(path)


def test_mark_sent_rejects_timezone_naive_timestamp() -> None:
    state = SentState()

    with pytest.raises(ValueError, match="timezone-aware"):
        state.mark_sent(
            ["https://example.com/model"], datetime(2026, 7, 18, 0, 30)
        )

    assert state.entries == {}


def test_save_removes_temporary_file_when_replace_fails(tmp_path, monkeypatch) -> None:
    path = tmp_path / "sent.json"
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    state = SentState()
    state.mark_sent(["https://example.com/model"], NOW)

    def fail_replace(source, destination) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("ai_daily.state.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failure"):
        state.save(path)

    assert not temporary_path.exists()
