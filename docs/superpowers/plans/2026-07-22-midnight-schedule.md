# Midnight Digest Schedule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the single daily GitHub Actions digest schedule at Asia/Shanghai 00:30.

**Architecture:** Change the workflow cron from UTC 00:30 to UTC 16:30, which maps to Shanghai 00:30 on the following calendar day. Lock the mapping with an offline workflow test and synchronize the Chinese operations documentation.

**Tech Stack:** GitHub Actions YAML, pytest, PyYAML, Chinese Markdown documentation.

## Global Constraints

- Keep exactly one scheduled cron.
- Use cron `30 16 * * *` for Asia/Shanghai 00:30.
- Do not alter manual dispatch, state isolation, model fallback, or DingTalk delivery behavior.
- Do not trigger a real DingTalk message during deployment verification.

---

### Task 1: Change and document the schedule

**Files:**
- Modify: `tests/test_workflows.py`
- Modify: `.github/workflows/daily.yml`
- Modify: `README.md`

**Interfaces:**
- Consumes GitHub Actions UTC cron syntax.
- Produces one scheduled trigger at `30 16 * * *` and Chinese documentation stating Beijing 00:30.

- [ ] **Step 1: Write the failing workflow assertion**

Change the schedule assertion in `test_daily_workflow_has_schedule_and_safe_manual_default` to:

```python
assert triggers["schedule"] == [{"cron": "30 16 * * *"}]
```

- [ ] **Step 2: Verify RED**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_workflows.py::test_daily_workflow_has_schedule_and_safe_manual_default -q
```

Expected: failure showing the workflow still contains `30 0 * * *`.

- [ ] **Step 3: Change the workflow cron**

Use exactly:

```yaml
schedule:
  - cron: "30 16 * * *"
```

- [ ] **Step 4: Update Chinese README text**

Replace every statement that the automatic schedule is Beijing 08:30 or UTC 00:30 with Beijing 00:30 and UTC 16:30. Replace troubleshooting guidance so the unique expected cron is `30 16 * * *`. Do not change examples for manually triggered runs.

- [ ] **Step 5: Verify GREEN and deploy**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_workflows.py -q
& '.venv\Scripts\python.exe' -m pytest -q
git diff --check
rg -n "08:30|30 0 \* \* \*|UTC 00:30" README.md .github/workflows/daily.yml tests/test_workflows.py
```

Expected: all tests pass, diff check is clean, and the obsolete schedule scan has no matches.

Commit and push:

```powershell
git add -- tests/test_workflows.py .github/workflows/daily.yml README.md
git commit -m "chore: move daily digest to midnight"
git push origin master
```

Confirm the pushed Test workflow succeeds. Do not dispatch `daily.yml`.
