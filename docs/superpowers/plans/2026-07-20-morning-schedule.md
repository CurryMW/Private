# Morning Digest Schedule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all three daily GitHub Actions digest triggers from 23:30/23:40/23:50 to 08:30/08:40/08:50 in Asia/Shanghai.

**Architecture:** Change only the UTC cron values and their matching tests and Chinese documentation. Preserve the existing three-trigger retry behavior, concurrency group, date-level delivery lock, manual-state isolation, AI configuration, and DingTalk delivery behavior.

**Tech Stack:** GitHub Actions YAML, Python 3.12, pytest, Chinese Markdown documentation.

## Global Constraints

- Use UTC cron because GitHub Actions does not accept an IANA timezone on this workflow.
- Map Shanghai 08:30/08:40/08:50 to UTC `00:30`/`00:40`/`00:50`.
- Keep all three compensation triggers and the existing `dingtalk-ai-daily` concurrency group.
- Do not change secrets, message content, model settings, state paths, or daily-once behavior.
- Push to `master` without force only after all tests pass.

---

### Task 1: Move the reliable schedule to morning

**Files:**
- Modify: `tests/test_workflows.py`
- Modify: `.github/workflows/daily.yml`
- Modify: `README.md`

**Interfaces:**
- Consumes: the approved Shanghai schedule 08:30/08:40/08:50.
- Produces: UTC cron entries `30 0 * * *`, `40 0 * * *`, and `50 0 * * *` with matching tests and documentation.

- [ ] **Step 1: Write the failing workflow expectation**

Change the schedule assertion in `tests/test_workflows.py` to:

```python
assert triggers["schedule"] == [
    {"cron": "30 0 * * *"},
    {"cron": "40 0 * * *"},
    {"cron": "50 0 * * *"},
]
```

- [ ] **Step 2: Run the targeted test and verify RED**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_workflows.py::test_daily_workflow_has_schedule_and_safe_manual_default -q
```

Expected: failure showing the deployed YAML still contains `30 15`, `40 15`, and `50 15`.

- [ ] **Step 3: Implement the UTC morning schedule**

Change `.github/workflows/daily.yml` to:

```yaml
on:
  schedule:
    - cron: "30 0 * * *"
    - cron: "40 0 * * *"
    - cron: "50 0 * * *"
```

- [ ] **Step 4: Run workflow tests and verify GREEN**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_workflows.py -q
```

Expected: all workflow tests pass.

- [ ] **Step 5: Update Chinese documentation**

Replace the user-facing schedule text in `README.md` with the exact values and explanations below:

```markdown
每天北京时间 08:30 开始自动运行，并在 08:40、08:50 提供补偿触发机会。
```

```markdown
`30 0 * * *`、`40 0 * * *`、`50 0 * * *` 分别表示每天 UTC 00:30、00:40、00:50，也就是 `Asia/Shanghai` 时区的 08:30、08:40、08:50。
```

Use this exact `status=empty` retry text:

```markdown
如果某次定时运行得到 `status=empty` 或运行失败，不会记录当日已完成，08:40 或 08:50 的后续触发仍会重新尝试。
```

Use this exact delayed-workflow cron text:

```markdown
确认三个 cron 仍是 `30 0 * * *`、`40 0 * * *`、`50 0 * * *`。不要把 cron 改成本地时间，因为 GitHub cron 使用 UTC。
```

- [ ] **Step 6: Run full local verification**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest -q
git diff --check
rg -n "23:30|23:40|23:50|30 15|40 15|50 15" .github/workflows/daily.yml tests/test_workflows.py README.md
```

Expected: all 125 tests pass, `git diff --check` succeeds, and the obsolete-schedule scan returns no matches.

- [ ] **Step 7: Commit and push**

```powershell
git add -- .github/workflows/daily.yml tests/test_workflows.py README.md
git commit -m "chore: move daily digest to morning"
git push origin master
```

Expected: the push fast-forwards `origin/master` without force.

- [ ] **Step 8: Verify GitHub deployment**

Read the deployed `.github/workflows/daily.yml` from the GitHub API and confirm all three UTC cron lines. Confirm the push-triggered `Test` workflow for the new head SHA completes with conclusion `success`; do not trigger a DingTalk workflow during verification.
