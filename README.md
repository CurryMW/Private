# 钉钉 AI 日报机器人

本项目每天收集最新的 AI 技术动态，过滤重复内容和纯商业新闻，再调用兼容 OpenAI Chat Completions 接口的模型，从候选内容中筛选并生成最多 8 条中文摘要，最后推送到钉钉自定义机器人。项目可部署在 GitHub 私有仓库中，通过 GitHub Actions 每天北京时间 08:30 自动运行。

日报重点关注模型发布、学术研究、开源工具、AI 工程实践和研发范式。融资、估值、股票、财报、人事、营销等缺少技术信息的内容会被过滤。每条消息都会保留原始来源链接，并把事实摘要和影响分析分开呈现。

> [!WARNING]
> 第三方模型接口会收到候选内容的标题、摘要、来源、URL、发布时间和程序生成的标识符。只有在你接受接口服务商的数据处理、保留和训练政策时，才应配置 `AI_BASE_URL`。除非服务商政策允许，否则不要在 `config/sources.yaml` 中加入私有或保密信息源。

## 文档目录

- [在本地预演一份日报](#local-preview)
- [部署到 GitHub 私有仓库](#github-deployment)
- [配置 GitHub Actions 密钥](#github-secrets)
- [验证预演和正式推送](#workflow-verification)
- [管理定时任务和状态缓存](#schedule-and-cache)
- [维护内容来源](#source-maintenance)
- [配置、CLI、工作流和来源参考](#reference)
- [架构与安全设计](#architecture-security)
- [故障排查](#troubleshooting)

<a id="local-preview"></a>

## 教程：在本地预演一份日报

本教程以 Windows 和 Python 3.12 为例，完成项目安装、离线测试和中文日报预演。预演模式不会发送钉钉消息，也不会修改已发送状态。

### 准备条件

- Windows PowerShell
- Git
- Python 3.12，其他 Python 版本不在当前支持范围内
- 模型服务商提供的 API Key
- 钉钉自定义机器人完整 Webhook，或者基础 Webhook 加 `access_token`

凭证只应保存在被 Git 忽略的 `.env` 文件中。不要把凭证写入 `.env.example`、命令参数、Git 提交或截图。

### 第 1 步：创建运行环境

在项目根目录执行：

```powershell
python --version
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

`python --version` 应显示 Python 3.12.x。用文本编辑器打开 `.env`，填写以下内容：

```dotenv
AI_API_KEY=
DINGTALK_WEBHOOK=
DINGTALK_ACCESS_TOKEN=
DRY_RUN=true
```

在未纳入 Git 管理的 `.env` 中填写真实值：

- `AI_API_KEY`：模型服务商提供的 API Key。
- `DINGTALK_WEBHOOK`：完整的 HTTPS Webhook，或者不含令牌的基础 Webhook。
- `DINGTALK_ACCESS_TOKEN`：如果完整 Webhook 已经包含 `access_token` 查询参数，可以留空；否则必须填写。

预演模式仍会校验必需凭证，但不会调用钉钉 Webhook。

### 第 2 步：离线验证安装

```powershell
python -m pytest -q
```

所有测试都应通过。测试会模拟全部外部服务，不会消耗模型额度、请求在线信息源或发送钉钉消息。

### 第 3 步：打印预演内容

```powershell
$env:DRY_RUN = "true"
try {
    python -m ai_daily.cli
} finally {
    Remove-Item Env:DRY_RUN -ErrorAction SilentlyContinue
}
```

命令会输出一个或多个 `--- preview N/M ---` 区块，最后一行类似：

```text
status=dry-run candidates=12 selected=6 parts=1
```

数量会随当前信息源变化。正常进度日志包括 `collected=N`、`prepared=N`、`selected=N`、`parts=N` 和 `status=dry-run`。预演内容会包含公开来源 URL，但不应出现 API Key、完整钉钉 Webhook 或 `access_token`。

### 完成结果

至此，本地环境已经能够采集、过滤、分析和渲染日报，同时不会推送消息或修改状态。修改默认值前请先阅读[环境变量参考](#environment-variables)；预演结果符合预期后，再按照[私有仓库部署指南](#github-deployment)上线。

<a id="github-deployment"></a>

## 部署指南：部署到 GitHub 私有仓库

下面提供 GitHub CLI 和网页界面两种方式，选择其中一种即可。仓库必须设置为私有，但私有仓库本身不能替代 GitHub Secrets，任何真实密钥都不能提交到 Git。

### 方式一：使用 GitHub CLI

#### 1. 安装 GitHub CLI

Windows PowerShell 执行：

```powershell
winget install --id GitHub.cli
```

安装完成后重新打开 PowerShell。如果系统没有 `winget`，请从 [GitHub CLI 官方网站](https://cli.github.com/)下载安装。

#### 2. 登录 GitHub

```powershell
gh auth login
gh auth status
```

登录时选择 GitHub.com、HTTPS 和浏览器授权。`gh auth status` 应显示当前已登录账户，不应报告认证失败。

#### 3. 检查远程仓库

在项目根目录执行：

```powershell
git remote -v
git status --short --branch
```

如果已经存在名为 `origin` 的远程仓库，不要再次执行 `gh repo create`。应先确认远程地址确实属于你的目标私有仓库。

#### 4. 创建并推送私有仓库

当前没有 `origin` 时执行：

```powershell
git branch -M main
gh repo create dingtalk-ai-daily --private --source=. --remote=origin --push
```

随后验证仓库属性：

```powershell
gh repo view --json nameWithOwner,visibility,defaultBranchRef --jq '"\(.nameWithOwner) \(.visibility) \(.defaultBranchRef.name)"'
git status --short --branch
```

输出应满足以下条件：

- 仓库可见性是 `PRIVATE`。
- 默认分支是 `main`。
- 本地 `main` 正在跟踪 `origin/main`。
- 工作区没有意外修改。

### 方式二：使用 GitHub 网页界面

1. 登录 GitHub，点击右上角 **+**，选择 **New repository**。也可以参考 GitHub 的[新建仓库说明](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository)。
2. 选择仓库所有者，仓库名称填写 `dingtalk-ai-daily`。
3. 可见性选择 **Private**。
4. 不要勾选 **Add a README file**、**Add .gitignore** 或许可证，确保远程仓库为空。
5. 点击 **Create repository**。
6. 复制 GitHub 显示的 HTTPS 仓库地址。
7. 在本地项目根目录执行以下命令，把 `<OWNER>` 替换为 GitHub 用户名或组织名：

   ```powershell
   git branch -M main
   git remote add origin https://github.com/<OWNER>/dingtalk-ai-daily.git
   git push -u origin main
   git status --short --branch
   ```

8. 打开仓库的 **Settings** > **General**，确认仓库是私有仓库，默认分支是 `main`。

<a id="github-secrets"></a>

## 配置 GitHub Actions 密钥

工作流读取以下仓库 Secrets：

| Secret 名称 | 是否必需 | 填写内容 |
| --- | --- | --- |
| `AI_API_KEY` | 是 | `https://apiclaude.cc` 服务生成的 API Key |
| `DINGTALK_WEBHOOK` | 是 | 钉钉自定义机器人的完整 HTTPS Webhook，或基础 Webhook |
| `DINGTALK_ACCESS_TOKEN` | 条件必需 | 完整 Webhook 不含 `access_token` 时填写；否则可以不创建 |

不要把真实值附加在 `gh secret set` 命令后面，也不要把它们发送到聊天、Issue 或日志中。

### 使用 GitHub CLI 配置

逐条执行以下命令。每条命令出现隐藏输入提示后，再粘贴对应的真实值：

```powershell
gh secret set AI_API_KEY
gh secret set DINGTALK_WEBHOOK
gh secret set DINGTALK_ACCESS_TOKEN
```

如果 `DINGTALK_WEBHOOK` 已经是包含非空 `access_token` 的完整 URL，请跳过第三条命令。

只查看 Secret 名称，不显示其值：

```powershell
gh secret list
```

### 使用 GitHub 网页界面配置

1. 打开私有仓库。
2. 进入 **Settings** > **Secrets and variables** > **Actions**。
3. 选择 **Secrets** 标签，然后点击 **New repository secret**。
4. 创建 `AI_API_KEY`。
5. 创建 `DINGTALK_WEBHOOK`。
6. 只有 Webhook 不包含 `access_token` 时，才创建 `DINGTALK_ACCESS_TOKEN`。

GitHub 保存 Secret 后不会再次显示原值。如果怀疑密钥泄漏，应在服务商或钉钉后台轮换密钥，再更新仓库 Secret，并检查 Git 历史和 Actions 日志。

<a id="workflow-verification"></a>

## 验证预演和正式推送

首次正式推送前，以及修改信息源、提示词、模型或工作流后，都应先运行预演。

### 使用 GitHub CLI 运行预演

```powershell
gh workflow run daily.yml -f dry_run=true
gh run list --workflow daily.yml --limit 1
$latestRunId = gh run list --workflow daily.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $latestRunId --exit-status
gh run view $latestRunId --log
```

确认以下结果：

- 任务成功并打印中文日报预览。
- 最后一行包含 `status=dry-run`。
- 钉钉群没有收到消息。
- 日志没有出现 API Key、完整 Webhook 或 `access_token`。
- 保存状态缓存的步骤被跳过。

### 使用 GitHub 网页界面运行预演

1. 打开仓库的 **Actions** 页面。
2. 选择 **Daily DingTalk Digest**。
3. 点击 **Run workflow**，分支选择 `main`。
4. 保持 **Print a preview without sending or saving state** 为启用状态。
5. 点击 **Run workflow**，打开新任务并检查 `digest` 作业日志。GitHub 的具体操作可参考[手动运行工作流说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow?tool=webui)。

### 使用 GitHub CLI 运行正式推送

下面的操作会向钉钉发送消息并更新已发送状态。只有预演内容安全且符合预期后才能执行：

```powershell
gh workflow run daily.yml -f dry_run=false
$latestRunId = gh run list --workflow daily.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $latestRunId --exit-status
gh run view $latestRunId --log
```

确认全部消息分片都到达目标群，最后一行包含 `status=sent`，任务状态为绿色，并且状态缓存保存步骤已经执行。只有所有消息分片都发送成功，程序才会更新状态。

如果使用网页界面，重复预演步骤并关闭 dry-run 输入。点击 **Run workflow** 后会立即开始正式推送，程序不会再次弹出确认提示。

<a id="schedule-and-cache"></a>

## 管理定时任务和状态缓存

`Daily DingTalk Digest` 工作流只使用一个 cron 表达式 `30 0 * * *`，表示每天 UTC 00:30，也就是 `Asia/Shanghai` 时区的 08:30。定时工作流从默认分支运行，因此 `.github/workflows/daily.yml` 必须存在于默认分支（当前仓库为 `master`），并且仓库 Actions 必须保持启用。

GitHub Actions 的定时任务在平台负载较高时可能延迟，具体可参考 GitHub 的[定时任务延迟说明](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows#scheduled-workflows-running-at-unexpected-times)。

定时运行是正式推送模式。工作流会设置 `DRY_RUN=false` 和 `ENFORCE_DAILY_ONCE=true`，使用名为 `dingtalk-ai-daily` 的并发组，并且不会取消已经运行中的任务。成功推送会记录北京时间日期；同一天再次手动重跑定时事件时会记录 `status=already-sent` 并在访问外部服务前成功退出。

工作流使用两类缓存：

- `actions/setup-python` 缓存 Python 依赖包。
- `actions/cache` 根据最新的 `dingtalk-ai-state-<OS>-` 前缀恢复整个 `.state` 目录；每次正式运行使用运行 ID 和重试次数生成唯一保存键。

定时任务的 URL 去重状态保存在 `.state/sent.json`，手动正式测试使用独立的 `.state/manual/sent.json`，因此测试不会提前消费当天的定时日报。每日成功日期保存在 `.state/deliveries.json`，并且只有定时任务会写入该文件。URL 状态只保存规范化 URL 的 SHA-256 哈希和带时区的时间戳，每日状态只保存 ISO 日期和带时区的成功时间；超过 30 天的记录会被清理。手动预演不会保存状态；只有全部钉钉消息发送成功且工作流步骤成功后才保存相应状态。

每次运行会先选择最近 36 小时内未发送的技术内容；没有时扩展到最近 7 天的未发送内容；仍没有时可发送最多 3 条并明确标记为“AI 近期技术回顾”；如果 7 天内没有任何通过质量过滤的可靠候选，则不调用模型，改发固定“今日状态”通知。内容模式的模型服务返回无效结果时，程序会以更小候选集重试一次；持续失败则发送“模型服务状态”通知，不保存 URL 状态。四种模式只要被钉钉完整接受，定时任务都会记录当天已完成。

GitHub 缓存只是优化手段，不是永久存储。缓存被清理、过期或恢复失败时，旧内容可能再次入选。可以进入 **Actions** > **Management** > **Caches** 查看缓存，参考 GitHub 的[缓存管理说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manage-caches)，或者执行：

```powershell
gh cache list
```

如果定时任务延迟，可以先手动运行预演检查当前内容；推送有时效要求时，再运行一次正式任务。不要绕过工作流并发控制，同时启动两个正式推送任务。

<a id="source-maintenance"></a>

## 维护内容来源

在项目根目录编辑 [`config/sources.yaml`](config/sources.yaml)。支持的来源配置如下：

```yaml
rss:
  - name: Official Feed
    url: https://news.example.com/feed.xml
arxiv:
  categories: [cs.AI, cs.LG]
  max_results: 40
huggingface_daily_papers:
  enabled: true
  limit_per_day: 20
github_repositories:
  - owner/repository
```

配置要求：

- RSS 应使用稳定的官方 RSS 或 Atom 地址。
- GitHub 仓库必须使用 `owner/repository` 格式，只采集正式 Release，跳过草稿和预发布版本。
- arXiv 至少配置一个分类，`max_results` 必须是正整数。
- Hugging Face 的 `limit_per_day` 必须是正整数。
- 不支持的字段会导致配置校验失败。
- 至少要配置并启用一个来源。

每次修改来源后执行：

```powershell
python -m pytest -q
$env:DRY_RUN = "true"
try {
    python -m ai_daily.cli
} finally {
    Remove-Item Env:DRY_RUN -ErrorAction SilentlyContinue
}
```

检查 `source failed: <label>: <ExceptionType>` 日志。单个来源失败会被隔离，其他来源继续执行，因此任务显示绿色并不代表每个来源都返回了数据。提交修改前，应检查预演中的信息源质量、重复过滤、纯商业新闻过滤和原始链接有效性。

<a id="reference"></a>

## 配置、CLI、工作流和来源参考

<a id="environment-variables"></a>

### 环境变量

CLI 会先加载当前工作目录中的 `.env`，再读取进程环境变量；已有进程环境变量的优先级更高。由于来源文件路径固定为 `config/sources.yaml`，请始终在项目根目录运行命令。

| 变量 | 是否必需 | 默认值 | 限制和作用 |
| --- | --- | --- | --- |
| `AI_API_KEY` | 是 | 无 | 非空的模型服务凭证，以 Bearer Token 形式发送。 |
| `AI_BASE_URL` | 否 | `https://apiclaude.cc/v1` | 兼容 OpenAI 的基础地址，程序请求 `<base>/chat/completions`。 |
| `AI_MODEL` | 否 | `claude-sonnet-4-6` | Chat Completions 请求使用的模型标识符。 |
| `DINGTALK_WEBHOOK` | 是 | 无 | 非空 HTTPS URL，可以包含一个非空 `access_token` 查询参数。 |
| `DINGTALK_ACCESS_TOKEN` | 条件必需 | 空 | Webhook 不包含有效 `access_token` 时必需；完整 Webhook 已提供令牌时忽略。 |
| `WINDOW_HOURS` | 否 | `36` | 正整数，用于确定优先选择新内容的时间范围和日报页脚。 |
| `FALLBACK_WINDOW_HOURS` | 否 | `168` | 不小于 `WINDOW_HOURS` 的正整数；新内容不足时扩展候选范围，默认最近 7 天。 |
| `MAX_ITEMS` | 否 | `8` | 1 到 8 之间的整数；模型返回超过该数量会校验失败。 |
| `MODEL_CANDIDATE_LIMIT` | 否 | `12` | 每次初始模型调用最多包含的候选数，必须不小于 `MAX_ITEMS`。 |
| `MODEL_RETRY_CANDIDATE_LIMIT` | 否 | `6` | 模型结构校验失败时的一次缩减重试候选数，必须不大于初始上限。 |
| `TIMEZONE` | 否 | `Asia/Shanghai` | 报告日期使用的 IANA 时区；未知时区会导致运行失败。 |
| `DRY_RUN` | 否 | `false` | 不区分大小写的 `1`、`true`、`yes` 或 `on` 表示真，其他值表示假。预演模式只打印内容，不发送或保存状态。 |
| `STATE_PATH` | 否 | `.state/sent.json` | 本地已发送状态 JSON 路径；保存时自动创建父目录。 |
| `DELIVERY_STATE_PATH` | 否 | `.state/deliveries.json` | 已成功推送的北京时间日期状态，仅定时任务启用每日一次保护时使用。 |
| `ENFORCE_DAILY_ONCE` | 否 | `false` | 定时任务设为 `true`；当天已经成功推送时记录 `status=already-sent` 并跳过。 |
| `GITHUB_TOKEN` | 否 | 空 | 用于提升 GitHub Releases API 限额；Actions 会自动提供只读 Job Token。 |

仓库中的 [`.env.example`](.env.example) 只包含变量名、安全默认值和空凭证字段。真实 `.env` 必须保持未跟踪状态。

### CLI

项目提供两种等价运行方式，不支持其他命令行参数：

```powershell
python -m ai_daily.cli
ai-daily
```

退出码 `0` 表示运行成功，并对应以下一种状态：

| 状态 | 含义 | 钉钉 | 状态文件 |
| --- | --- | --- | --- |
| `dry-run` | 已生成日报预演分片 | 不调用 | 不修改 |
| `sent` | 钉钉已接受全部消息分片，包括固定状态通知 | 顺序发送 | 内容消息保存入选 URL；启用每日保护时记录当天成功 |
| `already-sent` | 当天定时消息已成功发送，无需重复 | 不调用 | 不修改 |

退出码 `1` 表示配置、分析、推送或文件处理失败。为避免泄漏凭证，涉及请求信息的错误会使用通用描述。

### 来源行为

| 来源 | 配置 | 采集行为 |
| --- | --- | --- |
| RSS/Atom | `rss[].name`、`rss[].url` | 读取带日期条目，清理 HTML，跳过无效或过期内容。 |
| arXiv | `arxiv.categories`、`arxiv.max_results` | 按提交日期排序查询，并把链接规范化为 `https://arxiv.org/abs/...`。 |
| Hugging Face Daily Papers | `enabled`、`limit_per_day` | 查询时间窗口覆盖的每个 UTC 日期，不需要认证令牌。 |
| GitHub Releases | `github_repositories[]` | 每个仓库最多读取 10 个 Release，跳过草稿、预发布和过期内容。 |

最多同时执行 8 个来源操作。RSS、arXiv 和 GitHub Releases 使用共享的 `httpx` 请求逻辑：单次请求超时 20 秒；连接错误、超时、HTTP 429 和 5xx 最多尝试 3 次，重试间隔分别为 1 秒和 2 秒。

Hugging Face 来源通过 `asyncio.to_thread` 调用 `HfApi.list_daily_papers`，程序没有为该调用额外添加相同的 20 秒超时和本地重试保证。任何永久失败的来源，包括 Hugging Face，都会记录来源标签和异常类型，然后被跳过。

程序先采集最近 `FALLBACK_WINDOW_HOURS` 的内容，并依次尝试四种模式：

1. `fresh`：最近 `WINDOW_HOURS`（默认 36 小时）内未发送且通过质量过滤的内容，最多 `MAX_ITEMS` 条。
2. `extended`：最近 `FALLBACK_WINDOW_HOURS`（默认 168 小时）内未发送且通过质量过滤的内容，最多 `MAX_ITEMS` 条。
3. `review`：最近 168 小时内已经发送过、但仍通过时间、技术主题、商业内容和重复过滤的内容，最多 3 条；消息会明确标记为“AI 近期技术回顾”。
4. `notice`：7 天内没有可靠候选时，跳过模型并发送固定“今日状态”通知，不生成任何来源或技术事实。

每个内容模式都会规范化 URL、移除跟踪参数和片段、保留同一 URL 的最新版本，并使用 0.92 相似度阈值删除近似重复标题。模型只能从最终候选中选择证据链接；为避免单次提示词过大，初始模型调用只使用最新的 `MODEL_CANDIDATE_LIMIT` 条候选。

### GitHub Actions 工作流

| 文件 | 显示名称 | 触发方式 | 作用 |
| --- | --- | --- | --- |
| [`.github/workflows/test.yml`](.github/workflows/test.yml) | `Test` | Push、Pull Request | 安装 Python 3.12 依赖并运行离线测试。 |
| [`.github/workflows/daily.yml`](.github/workflows/daily.yml) | `Daily DingTalk Digest` | 每日 cron、手动触发 | 恢复状态、运行日报，并在非预演成功后保存状态。 |

两个工作流的仓库内容权限都是只读，并且第三方 Action 都固定到完整的提交 SHA。每日工作流的手动输入参数为布尔值 `dry_run`，默认值是 `true`。

<a id="architecture-security"></a>

## 架构与安全设计

### 数据流程

```text
sources.yaml + 环境变量
             |
             v
 RSS / arXiv / Hugging Face / GitHub Releases
             |
             v
规范化 -> 时间过滤 -> 已发送过滤 -> 商业内容过滤 -> 去重
             |
             v
兼容 OpenAI 的模型 -> 数据结构校验 + 证据 URL 校验
             |
             v
转义钉钉 Markdown -> 仅在完整条目之间分片
             |
             +--> DRY_RUN：打印预演并结束
             |
             +--> 正式运行：发送全部分片 -> 保存 URL 哈希状态 -> 缓存状态
```

模型只能选择候选证据中存在的 URL。返回值必须符合严格的日报数据结构：包含 1 到 8 条内容、2 到 3 条趋势、满足字段长度限制，并且不超过 `MAX_ITEMS`。这些限制可以减少虚构内容，但不能证明生成文字一定正确。修改模型或信息源后，应人工检查预演。

钉钉文本字段会进行空白规范化和 Markdown 标点转义，链接目标会做百分号编码。单条消息最多 18,000 个字符，并且只在完整条目之间拆分。当前仅支持未加签的钉钉自定义机器人，不支持要求签名的机器人。

### 密钥边界

- `.env` 和 `.state/` 已被 Git 忽略。
- GitHub Secrets 只进入 CLI 运行步骤，不会传给依赖安装或缓存步骤。
- 工作流 Job Token 只有仓库内容只读权限，仅用于 GitHub Releases 请求。
- 模型和钉钉凭证使用支持隐藏值的配置类型。
- HTTP 依赖日志被限制在 warning 及以上级别。
- 来源错误只记录配置标签和异常类名。
- 模型和钉钉错误不会输出响应正文、请求 URL、Webhook 查询参数或上游错误消息。
- 已发送状态只保存 URL 哈希和时间戳，不保存完整 URL、消息正文或凭证。
- 预演模式不会创建钉钉发送器，也不会更新状态。
- 正式运行只有在所有钉钉消息分片成功后才保存状态。

私有仓库只能减少代码暴露，不能充当密钥存储。仓库协作者权限、Actions 权限、第三方 Action、日志、缓存、构建产物和服务商后台都是独立的信任边界。应定期检查访问权限，并轮换任何可能泄漏的凭证。

### 本地验收和密钥扫描

发布文档或工作流修改前执行：

```powershell
python -m pytest -q
$secretPatterns = 'sk-' + '[A-Za-z0-9]{12,}|access_' + 'token=[A-Za-z0-9_-]{12,}|AI_API_' + 'KEY=.+'
$knownSyntheticPatterns = @(
    ('^tests/test_dingtalk\.py:\d+:\s+dingtalk_access_' + 'token=access_token,$'),
    ('^tests/test_sources\.py:\d+:\s+raise RuntimeError\("https://private\.example/feed\?access_' + 'token=secret-value"\)$')
)
$secretFindings = git grep -n -E $secretPatterns -- ':!docs/superpowers/**' ':!.env.example' |
    Where-Object {
        $matchedLine = $_
        -not ($knownSyntheticPatterns | Where-Object { $matchedLine -match $_ })
    }
if ($secretFindings) {
    $secretFindings
    throw '发现疑似密钥内容。'
}
git status --short
```

测试必须在不访问外部网络的情况下通过，密钥扫描必须没有输出。扫描仅过滤两个精确锚定的测试占位：变量名赋值和特意构造的模拟 URL；测试或源码中其他类似密钥的内容仍会被报告。提交前，`git status` 应只显示本次预期修改的文件。

<a id="troubleshooting"></a>

## 故障排查

### 出现 `analysis validation failed` 或模型 JSON 无效

模型接口返回的内容不是有效日报 JSON，或者违反数据结构约束。请确认：

- `AI_BASE_URL` 指向兼容 OpenAI Chat Completions 的接口。
- `AI_MODEL` 在该服务中存在。
- 助手返回内容是纯 JSON，或者只在外层包裹一个 JSON 代码块。
- 返回值包含 `overview`、1 到 8 个有效 `items` 和 2 到 3 个 `trends`。
- 每个条目 URL 经过规范化后都能与候选证据中的 URL 精确对应。

不要绕过校验。程序会以更小候选集自动重试一次；若仍然无效，则改发模型服务状态通知。应修正接口地址、模型或提示词兼容性，再重新运行预演。

### HTTP 401

模型接口返回 401 或 403 时不会重试模型请求，日报会发送模型服务状态通知。401 通常表示 `AI_API_KEY`、`AI_BASE_URL` 或服务商授权不正确；403 还可能表示额度、模型权限或服务商访问限制。来源返回 401 时会记录 `source failed`，其他来源仍继续执行，应检查对应 Feed 或 GitHub 访问权限。钉钉 HTTP 授权失败会显示通用错误 `DingTalk delivery failed`。

请通过 `.env` 或仓库 Secret 的隐藏输入重新填写凭证，不要打印凭证。疑似泄漏时应轮换密钥，不要把密钥粘贴到 Issue 或日志中。

### HTTP 429 或 5xx

使用 `httpx` 的请求路径会对临时错误最多尝试 3 次，并使用短暂间隔：

- RSS、arXiv 和 GitHub Releases 的单次请求超时为 20 秒。
- 模型分析超时为 180 秒。
- 钉钉推送超时为 20 秒。

模型持续失败会发送模型服务状态通知；钉钉持续失败会使任务失败；`httpx` 来源持续失败只会跳过该来源。Hugging Face 使用 `asyncio.to_thread` 调用 `HfApi.list_daily_papers`，没有应用相同的本地超时和重试策略，但失败仍会被隔离并跳过。HTTP 403、超时或模型 JSON 校验失败时，先检查模型服务商的额度、权限和可用模型。

### 出现 `DingTalk rejected the message` 或非零 `errcode`

这表示钉钉返回了 HTTP 成功，但业务 `errcode` 非零或格式无效。程序会隐藏钉钉响应消息，因为响应可能回显 Webhook 信息。

请在钉钉机器人设置中确认：

- 机器人已启用并且是未加签模式。
- 机器人允许在目标群中使用。
- 使用的是当前有效令牌。
- Webhook 恰好包含一个非空 `access_token`，或者使用基础 Webhook 配合 `DINGTALK_ACCESS_TOKEN`。

然后通过隐藏输入更新仓库 Secrets，先预演，再正式推送。

### 收到“今日状态”通知

这表示最近 7 天内没有获取到通过质量过滤的可靠候选。程序没有调用模型生成未经来源支持的信息，而是向钉钉发送固定状态文字。只要钉钉接受消息，定时任务仍会返回 `status=sent` 并记录当天已经完成。可以检查 `collected`、`prepared`、`mode=notice`、来源失败日志和 `config/sources.yaml`；无需为了“有内容”而降低证据校验标准。

### 出现 `status=already-sent`

当天已有一个定时运行完成了全部钉钉发送，因此再次运行时会在抓取、模型和钉钉调用之前正常跳过。这是每日一次保护的预期行为，不是错误。手动运行不会写入每日成功日期。

### 定时工作流延迟或没有出现

GitHub cron 不是精确调度器，平台负载较高时可能延迟。确认 Actions 已启用，`daily.yml` 位于仓库默认分支，并且唯一 cron 是 `30 0 * * *`。不要把 cron 改成本地时间，因为 GitHub cron 使用 UTC。如果定时运行没有出现，需要及时推送时，可以手动预演后再启动正式任务。

### 状态文件损坏或旧内容重复出现

删除或修复本地状态文件前，先确认 `STATE_PATH` 和 `DELIVERY_STATE_PATH` 指向正确文件。程序会拒绝包含原始 URL、无效哈希、无效日期或不带时区时间戳的状态文件。在 Actions 中，缓存未命中或被清理可能使旧内容重新符合条件。请检查 **Actions** > **Caches**，先运行预演，并且不要把缓存当作永久存储。

## 许可证

当前没有声明许可证。在仓库所有者添加许可证前，应将本项目视为私有、保留所有权利的代码。
