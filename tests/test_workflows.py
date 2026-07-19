import re
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTION_PINS = {
    "actions/cache": "caa296126883cff596d87d8935842f9db880ef25",
    "actions/checkout": "df4cb1c069e1874edd31b4311f1884172cec0e10",
    "actions/setup-python": "ece7cb06caefa5fff74198d8649806c4678c61a1",
}


def _load_workflow(name: str) -> dict[str, Any]:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)

    # PyYAML follows YAML 1.1 and parses the unquoted GitHub Actions key `on`
    # as the boolean True. Normalize that parser quirk before inspecting it.
    if True in data and "on" not in data:
        data["on"] = data.pop(True)
    return data


def _steps(workflow: dict[str, Any], job_name: str) -> list[dict[str, Any]]:
    return workflow["jobs"][job_name]["steps"]


def _step_using(steps: list[dict[str, Any]], action: str) -> dict[str, Any]:
    return next(
        step
        for step in steps
        if str(step.get("uses", "")).partition("@")[0] == action
    )


def _assert_action_is_pinned(step: dict[str, Any], action: str) -> None:
    uses = step["uses"]
    assert re.fullmatch(rf"{re.escape(action)}@[0-9a-f]{{40}}", uses)
    action_repository = "/".join(action.split("/")[:2])
    assert uses == f"{action}@{ACTION_PINS[action_repository]}"


def _step_identity(step: dict[str, Any]) -> str:
    if "uses" in step:
        return str(step["uses"]).partition("@")[0]
    return str(step["run"])


def test_test_workflow_runs_for_pushes_and_pull_requests() -> None:
    workflow = _load_workflow("test.yml")

    assert set(workflow["on"]) == {"push", "pull_request"}
    steps = _steps(workflow, "test")
    assert any(step.get("run") == "python -m pytest -q" for step in steps)


def test_both_workflows_pin_current_python_actions() -> None:
    for name, job_name in (("test.yml", "test"), ("daily.yml", "digest")):
        steps = _steps(_load_workflow(name), job_name)
        checkout = _step_using(steps, "actions/checkout")
        _assert_action_is_pinned(checkout, "actions/checkout")
        setup = _step_using(steps, "actions/setup-python")
        _assert_action_is_pinned(setup, "actions/setup-python")
        assert setup["with"]["python-version"] == "3.12"
        assert setup["with"]["cache"] == "pip"


def test_workflow_steps_have_explicit_safe_ordering() -> None:
    test_steps = _steps(_load_workflow("test.yml"), "test")
    assert [_step_identity(step) for step in test_steps] == [
        "actions/checkout",
        "actions/setup-python",
        'python -m pip install -e ".[dev]"',
        "python -m pytest -q",
    ]

    daily_steps = _steps(_load_workflow("daily.yml"), "digest")
    assert [_step_identity(step) for step in daily_steps] == [
        "actions/checkout",
        "actions/setup-python",
        'python -m pip install -e ".[dev]"',
        "actions/cache/restore",
        "python -m ai_daily.cli",
        "actions/cache/save",
    ]


def test_daily_workflow_has_schedule_and_safe_manual_default() -> None:
    workflow = _load_workflow("daily.yml")
    triggers = workflow["on"]

    assert workflow["name"] == "钉钉 AI 技术日报"
    assert triggers["schedule"] == [
        {"cron": "30 15 * * *"},
        {"cron": "40 15 * * *"},
        {"cron": "50 15 * * *"},
    ]
    dry_run = triggers["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run["description"] == "只预览，不发送钉钉消息，也不保存状态"
    assert dry_run["type"] == "boolean"
    assert dry_run["default"] is True


def test_daily_workflow_has_read_only_permissions_and_concurrency() -> None:
    workflow = _load_workflow("daily.yml")

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "dingtalk-ai-daily",
        "cancel-in-progress": False,
    }


def test_daily_job_installs_runs_and_maps_secrets_safely() -> None:
    workflow = _load_workflow("daily.yml")
    job = workflow["jobs"]["digest"]

    assert job["env"] == {
        "AI_BASE_URL": "https://apiclaude.cc/v1",
        "AI_MODEL": "claude-sonnet-4-6",
        "DRY_RUN": "${{ github.event_name == 'workflow_dispatch' && inputs.dry_run || 'false' }}",
        "STATE_PATH": "${{ github.event_name == 'schedule' && '.state/sent.json' || '.state/manual/sent.json' }}",
        "DELIVERY_STATE_PATH": ".state/deliveries.json",
        "ENFORCE_DAILY_ONCE": "${{ github.event_name == 'schedule' && 'true' || 'false' }}",
    }
    steps = job["steps"]
    assert any(step.get("run") == 'python -m pip install -e ".[dev]"' for step in steps)
    cli_step = next(step for step in steps if step.get("run") == "python -m ai_daily.cli")
    assert cli_step["env"] == {
        "AI_API_KEY": "${{ secrets.AI_API_KEY }}",
        "DINGTALK_WEBHOOK": "${{ secrets.DINGTALK_WEBHOOK }}",
        "DINGTALK_ACCESS_TOKEN": "${{ secrets.DINGTALK_ACCESS_TOKEN }}",
        "GITHUB_TOKEN": "${{ github.token }}",
    }
    assert all("env" not in step for step in steps if step is not cli_step)


def test_daily_state_cache_uses_unique_keys_and_only_saves_live_success() -> None:
    steps = _steps(_load_workflow("daily.yml"), "digest")

    restore = _step_using(steps, "actions/cache/restore")
    _assert_action_is_pinned(restore, "actions/cache/restore")
    assert restore["with"] == {
        "path": ".state",
        "key": (
            "dingtalk-ai-state-${{ runner.os }}-${{ github.run_id }}-"
            "${{ github.run_attempt }}"
        ),
        "restore-keys": "dingtalk-ai-state-${{ runner.os }}-",
    }

    save = _step_using(steps, "actions/cache/save")
    _assert_action_is_pinned(save, "actions/cache/save")
    assert save["if"] == (
        "success() && !(github.event_name == 'workflow_dispatch' && inputs.dry_run)"
    )
    assert save["with"] == {
        "path": ".state",
        "key": (
            "dingtalk-ai-state-${{ runner.os }}-${{ github.run_id }}-"
            "${{ github.run_attempt }}"
        ),
    }


def test_pinned_actions_keep_inline_version_comments() -> None:
    for workflow_name in ("test.yml", "daily.yml"):
        text = (WORKFLOWS / workflow_name).read_text(encoding="utf-8")
        assert (
            f"actions/checkout@{ACTION_PINS['actions/checkout']} # v6" in text
        )
        assert (
            f"actions/setup-python@{ACTION_PINS['actions/setup-python']} # v6" in text
        )

    daily = (WORKFLOWS / "daily.yml").read_text(encoding="utf-8")
    assert daily.count(f"@{ACTION_PINS['actions/cache']} # v5") == 2
