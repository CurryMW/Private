# Daily Delivery Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the GitHub-hosted DingTalk digest retry at 23:30, 23:40, and 23:50 while sending at most once per Shanghai calendar date and keeping manual-test URL history separate from scheduled-delivery history.

**Architecture:** Add a focused `DeliveryState` JSON store for completed report dates, then gate scheduled runs in `run_digest` before any network work. Keep the existing production URL state at `.state/sent.json`, route manual runs to `.state/manual/sent.json`, and use three serialized GitHub cron events so later runs safely skip after the first successful delivery.

**Tech Stack:** Python 3.12, Pydantic 2, pytest/pytest-asyncio, GitHub Actions YAML, actions/cache v5.

## Global Constraints

- Continue using GitHub Actions only; do not add servers, cloud functions, databases, or paid schedulers.
- Use `Asia/Shanghai` for the report date and UTC cron values `15:30`, `15:40`, and `15:50`.
- Do not change the AI provider, model, sources, filtering rules, or DingTalk message format.
- Do not send an empty digest when no eligible candidate exists.
- Never write API keys, the complete DingTalk webhook, or access tokens to files or logs.
- All production-code behavior changes require a failing test observed before implementation.

---

### Task 1: Completed-date state store

**Files:**
- Create: `src/ai_daily/delivery_state.py`
- Create: `tests/test_delivery_state.py`

**Interfaces:**
- Consumes: `datetime.date`, timezone-aware `datetime.datetime`, and a filesystem path.
- Produces: `DeliveryState.load(path)`, `is_delivered(report_date)`, `mark_delivered(report_date, delivered_at)`, and `save(path)`.

- [ ] **Step 1: Write failing state tests**

Create `tests/test_delivery_state.py` with these behaviors:

```python
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
```

- [ ] **Step 2: Run the state tests and verify RED**

Run:

```powershell
python -m pytest tests/test_delivery_state.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'ai_daily.delivery_state'`.

- [ ] **Step 3: Implement the minimal state store**

Create `src/ai_daily/delivery_state.py`:

```python
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
```

- [ ] **Step 4: Run state tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_delivery_state.py tests/test_state.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the state store**

```powershell
git add -- src/ai_daily/delivery_state.py tests/test_delivery_state.py
git commit -m "feat: track completed daily deliveries"
```

---

### Task 2: Configuration and application daily gate

**Files:**
- Modify: `src/ai_daily/config.py`
- Modify: `src/ai_daily/app.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: `DeliveryState` from Task 1 and environment variables `DELIVERY_STATE_PATH` and `ENFORCE_DAILY_ONCE`.
- Produces: `Settings.delivery_state_path: Path`, `Settings.enforce_daily_once: bool`, and `RunResult.status == "already-sent"` for a protected duplicate date.

- [ ] **Step 1: Write failing configuration tests**

Extend the existing default and environment tests in `tests/test_config.py` with these exact assertions:

```python
assert settings.delivery_state_path == Path(".state/deliveries.json")
assert settings.enforce_daily_once is False
```

Add these environment values to the configured mapping and assert them:

```python
"DELIVERY_STATE_PATH": ".state/custom-deliveries.json",
"ENFORCE_DAILY_ONCE": "true",
```

```python
assert settings.delivery_state_path == Path(".state/custom-deliveries.json")
assert settings.enforce_daily_once is True
```

- [ ] **Step 2: Run configuration tests and verify RED**

Run:

```powershell
python -m pytest tests/test_config.py -q
```

Expected: failure because `Settings` has no `delivery_state_path` or `enforce_daily_once` fields.

- [ ] **Step 3: Implement configuration fields**

Add to `Settings` in `src/ai_daily/config.py`:

```python
delivery_state_path: Path = Path(".state/deliveries.json")
enforce_daily_once: bool = False
```

Pass them from `load_settings`:

```python
delivery_state_path=source.get(
    "DELIVERY_STATE_PATH", ".state/deliveries.json"
),
enforce_daily_once=_parse_bool(source.get("ENFORCE_DAILY_ONCE")),
```

- [ ] **Step 4: Run configuration tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_config.py -q
```

Expected: all configuration tests pass.

- [ ] **Step 5: Write failing application tests**

Update the `settings` helper in `tests/test_app.py` to accept `enforce_daily_once` and pass both new settings:

```python
def settings(
    tmp_path,
    *,
    dry_run: bool = False,
    enforce_daily_once: bool = False,
) -> Settings:
    return Settings(
        ai_api_key="test-ai-key",
        dingtalk_webhook=(
            "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        ),
        window_hours=36,
        timezone="Asia/Shanghai",
        dry_run=dry_run,
        state_path=tmp_path / "sent.json",
        delivery_state_path=tmp_path / "deliveries.json",
        enforce_daily_once=enforce_daily_once,
        github_token="test-github-token",
    )
```

Import `DeliveryState` and add the duplicate-date behavior:

```python
@pytest.mark.asyncio
async def test_completed_report_date_skips_before_network_work(
    tmp_path, monkeypatch
) -> None:
    run_settings = settings(tmp_path, enforce_daily_once=True)
    delivery_state = DeliveryState()
    delivery_state.mark_delivered(NOW.date(), NOW_UTC)
    delivery_state.save(run_settings.delivery_state_path)

    def fail_client(**kwargs):
        raise AssertionError("HTTP client must not be created")

    monkeypatch.setattr(app.httpx, "AsyncClient", fail_client)

    result = await app.run_digest(
        run_settings,
        SourceConfig(github_repositories=["owner/repository"]),
        now=NOW,
    )

    assert result == app.RunResult(
        status="already-sent",
        candidate_count=0,
        selected_count=0,
        part_count=0,
    )
```

Change the first line inside the existing `test_normal_run_uses_one_client_and_saves_only_selected_urls` test to enable the protected production path:

```python
run_settings = settings(tmp_path, enforce_daily_once=True)
```

Add these final assertions to that test:

```python
assert result.status == "sent"
assert DeliveryState.load(
    run_settings.delivery_state_path
).is_delivered(NOW.date())
```

Change the settings construction in the existing dry-run, empty-candidate, and delivery-failure tests so the protection path is active:

```python
run_settings = settings(tmp_path, dry_run=True, enforce_daily_once=True)
```

```python
run_settings = settings(tmp_path, enforce_daily_once=True)
```

Then add this assertion to each of those three tests:

```python
assert not run_settings.delivery_state_path.exists()
```

- [ ] **Step 6: Run application tests and verify RED**

Run:

```powershell
python -m pytest tests/test_app.py -q
```

Expected: failures because `RunResult` does not accept `already-sent` and `run_digest` does not load or save `DeliveryState`.

- [ ] **Step 7: Implement the daily gate and success marker**

In `src/ai_daily/app.py`, import `DeliveryState`, extend the result literal, calculate `report_date` before opening the HTTP client, and add the early return:

```python
from ai_daily.delivery_state import DeliveryState


@dataclass(frozen=True)
class RunResult:
    status: Literal["sent", "dry-run", "empty", "already-sent"]
    candidate_count: int
    selected_count: int
    part_count: int
```

```python
report_date = run_at.astimezone(ZoneInfo(settings.timezone)).date()
delivery_state = DeliveryState()
if settings.enforce_daily_once:
    delivery_state = DeliveryState.load(settings.delivery_state_path)
    if delivery_state.is_delivered(report_date):
        logger.info("status=already-sent")
        return RunResult(
            status="already-sent",
            candidate_count=0,
            selected_count=0,
            part_count=0,
        )
```

Remove the later duplicate `report_date` calculation. After all DingTalk parts send and the URL state saves, add:

```python
if settings.enforce_daily_once:
    delivery_state.mark_delivered(report_date, run_at)
    delivery_state.save(settings.delivery_state_path)
```

- [ ] **Step 8: Run application and configuration tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_app.py tests/test_config.py tests/test_delivery_state.py -q
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit the application gate**

```powershell
git add -- src/ai_daily/config.py src/ai_daily/app.py tests/test_config.py tests/test_app.py
git commit -m "feat: prevent duplicate scheduled digests"
```

---

### Task 3: Retry schedule and manual-state isolation

**Files:**
- Modify: `.github/workflows/daily.yml`
- Modify: `tests/test_workflows.py`

**Interfaces:**
- Consumes: settings fields from Task 2.
- Produces: three scheduled triggers, production/manual `STATE_PATH` routing, and schedule-only `ENFORCE_DAILY_ONCE=true`.

- [ ] **Step 1: Write failing workflow tests**

Change the schedule assertion in `tests/test_workflows.py` to:

```python
assert triggers["schedule"] == [
    {"cron": "30 15 * * *"},
    {"cron": "40 15 * * *"},
    {"cron": "50 15 * * *"},
]
```

Change the expected job environment to:

```python
assert job["env"] == {
    "AI_BASE_URL": "https://apiclaude.cc/v1",
    "AI_MODEL": "claude-sonnet-4-6",
    "DRY_RUN": "${{ github.event_name == 'workflow_dispatch' && inputs.dry_run || 'false' }}",
    "STATE_PATH": "${{ github.event_name == 'schedule' && '.state/sent.json' || '.state/manual/sent.json' }}",
    "DELIVERY_STATE_PATH": ".state/deliveries.json",
    "ENFORCE_DAILY_ONCE": "${{ github.event_name == 'schedule' && 'true' || 'false' }}",
}
```

- [ ] **Step 2: Run workflow tests and verify RED**

Run:

```powershell
python -m pytest tests/test_workflows.py -q
```

Expected: failures showing only one cron and the three missing environment variables.

- [ ] **Step 3: Implement workflow scheduling and routing**

Update `.github/workflows/daily.yml`:

```yaml
on:
  schedule:
    - cron: "30 15 * * *"
    - cron: "40 15 * * *"
    - cron: "50 15 * * *"
```

Extend `jobs.digest.env`:

```yaml
      STATE_PATH: ${{ github.event_name == 'schedule' && '.state/sent.json' || '.state/manual/sent.json' }}
      DELIVERY_STATE_PATH: .state/deliveries.json
      ENFORCE_DAILY_ONCE: ${{ github.event_name == 'schedule' && 'true' || 'false' }}
```

- [ ] **Step 4: Run workflow tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_workflows.py -q
```

Expected: all workflow tests pass.

- [ ] **Step 5: Commit workflow reliability**

```powershell
git add -- .github/workflows/daily.yml tests/test_workflows.py
git commit -m "fix: add redundant daily schedule"
```

---

### Task 4: Chinese documentation, full verification, and deployment

**Files:**
- Modify: `README.md`
- Verify: all project files and GitHub Actions runs.

**Interfaces:**
- Consumes: completed code and workflow behavior from Tasks 1–3.
- Produces: accurate Chinese deployment/troubleshooting instructions and a deployed GitHub default branch.

- [ ] **Step 1: Update the Chinese README**

Change the scheduling section to state that the workflow has three UTC triggers—`30 15 * * *`, `40 15 * * *`, and `50 15 * * *`—corresponding to 23:30, 23:40, and 23:50 in Shanghai. Document these exact environment variables in the configuration table:

```markdown
| `DELIVERY_STATE_PATH` | 否 | `.state/deliveries.json` | 已成功推送的北京时间日期状态，仅定时任务启用每日一次保护时使用。 |
| `ENFORCE_DAILY_ONCE` | 否 | `false` | 定时任务设为 `true`；当天已经成功推送时记录 `status=already-sent` 并跳过。 |
```

Document that scheduled runs use `.state/sent.json`, manual runs use `.state/manual/sent.json`, `empty` does not lock the date, and successful manual tests do not consume the scheduled report.

- [ ] **Step 2: Run the complete local test suite**

Run:

```powershell
python -m pytest -q
```

Expected: every test passes with no warnings caused by the changed code.

- [ ] **Step 3: Inspect the final diff and secrets**

Run:

```powershell
git diff --check
git status --short
rg -n "7b4efa8c5a968e059fbcf8518f38a3d2a33bd0a82f73cfb5c38f167d97fe07d1|SEC[0-9A-Za-z]+|sk-[0-9A-Za-z]" . --glob '!docs/superpowers/plans/*'
```

Expected: `git diff --check` succeeds, only intended files are modified, and the secret scan returns no matches.

- [ ] **Step 4: Commit documentation**

```powershell
git add -- README.md
git commit -m "docs: explain daily delivery retries"
```

- [ ] **Step 5: Push the commits to GitHub**

```powershell
git push origin master
```

Expected: `origin/master` advances without a force push.

- [ ] **Step 6: Verify GitHub CI and deployed workflow**

Use the GitHub Actions API to confirm:

- the push-triggered `Test` workflow completes successfully;
- the deployed `.github/workflows/daily.yml` contains all three cron entries;
- the deployed workflow state is `active`;
- no production DingTalk message is sent during this verification.

- [ ] **Step 7: Perform the approved recovery send separately**

Before sending a real DingTalk recovery message, report the deployed verification result and request explicit confirmation for a one-time `workflow_dispatch` with `dry_run=false`. A recovery send is external communication and must not be bundled into the code deployment.
