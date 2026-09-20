# AI 情报摘要与 GitHub 趋势报告

本项目包含两个相互独立的流程：`AI 情报摘要`只通过百度官方搜索 API 发现候选，再由 TeamoRouter `gpt-5.6-luna` 生成最多 8 条中文摘要；`GitHub 趋势报告`使用 GitHub 官方 API 维护独立基线，观察周期满 72 小时后生成最多 5 个项目的报告。AI 情报工作流设计为每天北京时间 08:30 运行；当前改动只在本地准备，未部署或远程启用定时任务。信息范围限于百度可检索的公开内容，不代表全球或社交平台完整覆盖。

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
- TeamoRouter 模型服务的 API Key
- 百度官方搜索 API Key
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
AI_BASE_URL=https://api.teamorouter.cn/v1
AI_MODEL=gpt-5.6-luna
BAIDU_SEARCH_API_KEY=
DINGTALK_WEBHOOK=
DINGTALK_ACCESS_TOKEN=
DRY_RUN=true
```

在未纳入 Git 管理的 `.env` 中填写真实值：

- `AI_API_KEY`：TeamoRouter 模型服务提供的 API Key。
- `AI_BASE_URL`：TeamoRouter OpenAI-compatible 基础地址。
- `AI_MODEL`：唯一生产模型，必须显式填写 `gpt-5.6-luna`。
- `BAIDU_SEARCH_API_KEY`：百度千帆 Web Search API 凭据，与模型凭据彻底分离。
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

数量会随百度可检索到的公开信息变化。正常进度日志包括 `collected=N` 和 `status=dry-run`，最终摘要显示候选数、入选数和消息分片数。预演可调用搜索和模型，但不构造钉钉发送器，不修改日报成功状态；它会更新独立百度每日用量记录，也不应出现 API Key、完整 Webhook 或 `access_token`。

### 完成结果

至此，本地环境可以预演 AI 情报摘要，同时不会推送消息或修改状态。本地 dry-run 通过不等于 GitHub Actions 已部署；要远程启用定时任务，仍需先配置 Secrets、手工触发 dry-run 并审查日志。

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
| `BAIDU_SEARCH_API_KEY` | 是 | 百度官方 Web Search API Key |
| `AI_API_KEY` | 是 | TeamoRouter 模型服务的 API Key |
| `DINGTALK_WEBHOOK` | 是 | 钉钉自定义机器人的完整 HTTPS Webhook，或基础 Webhook |
| `DINGTALK_ACCESS_TOKEN` | 条件必需 | 完整 Webhook 不含 `access_token` 时填写；否则可以不创建 |

不要把真实值附加在 `gh secret set` 命令后面，也不要把它们发送到聊天、Issue 或日志中。

### 使用 GitHub CLI 配置

逐条执行以下命令。每条命令出现隐藏输入提示后，再粘贴对应的真实值：

```powershell
gh secret set BAIDU_SEARCH_API_KEY
gh secret set AI_API_KEY
gh secret set DINGTALK_WEBHOOK
gh secret set DINGTALK_ACCESS_TOKEN
```

如果 `DINGTALK_WEBHOOK` 已经是包含非空 `access_token` 的完整 URL，请跳过第四条命令。

只查看 Secret 名称，不显示其值：

```powershell
gh secret list
```

### 使用 GitHub 网页界面配置

1. 打开私有仓库。
2. 进入 **Settings** > **Secrets and variables** > **Actions**。
3. 选择 **Secrets** 标签，然后点击 **New repository secret**。
4. 创建 `BAIDU_SEARCH_API_KEY`。
5. 创建 `AI_API_KEY`。
6. 创建 `DINGTALK_WEBHOOK`。
7. 只有 Webhook 不包含 `access_token` 时，才创建 `DINGTALK_ACCESS_TOKEN`。

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
2. 选择 **AI 情报摘要**。
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

`AI 情报摘要` 工作流只使用一个 cron 表达式 `30 0 * * *`，表示每天 UTC 00:30，也就是 `Asia/Shanghai` 时区的 08:30。定时工作流只会在相关改动推送到默认分支且仓库 Actions 启用后生效；仅在本地提交该配置不等于已部署。

GitHub Actions 的定时任务在平台负载较高时可能延迟，具体可参考 GitHub 的[定时任务延迟说明](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows#scheduled-workflows-running-at-unexpected-times)。

定时运行是正式推送模式。工作流会设置 `DRY_RUN=false` 和 `ENFORCE_DAILY_ONCE=true`，使用名为 `dingtalk-ai-digest` 的独立并发组，不会取消已在运行的任务。只有百度搜索、证据筛选、模型生成、消息校验和钉钉全部分片发送成功后，程序才记录 URL 与当日成功状态。

工作流使用两类缓存：

- `actions/setup-python` 缓存 Python 依赖包。
- `actions/cache` 优先恢复 `dingtalk-ai-digest-state-<OS>-` 独立前缀，并保留旧 `dingtalk-ai-state-<OS>-` 前缀作为一次性迁移来源，使已发送 URL 历史继续生效。

定时和手工正式运行共用 `.state/sent.json` 中的 30 天 URL 去重历史，避免人工验收后又重复发送。定时运行的当日成功日期保存在 `.state/deliveries.json`。URL 历史只保存规范化 URL 的 SHA-256 哈希、事件签名和带时区的时间戳；损坏的旧状态会使任务失败，不会被当作空历史继续发送。

每次 AI 情报运行最多发起 20 次百度搜索（16 个固定中英文主题加 4 个轮换热点），只处理最近 36 小时的候选。没有合格内容时任务成功结束但钉钉保持静默，不写成功状态；搜索、模型或钉钉失败时任务非零退出，不发送错误、降级或运行状态通知。百度限额或免费额度耗尽也按搜索失败处理，不自动改用后付费或其他搜索来源。独立用量账本还会跨运行执行每日 20 次累计上限。

每次运行最多发起 20 次百度搜索，同时通过独立的 `.state/baidu-search-usage.json` 按北京时间日期累计限制为每天 20 次。每次请求在发出前预留并保存额度，因此空结果、dry-run、失败请求和成功请求都会计入；达到上限后不再调用百度。该账本不属于日报成功状态，Actions 必须缓存并恢复 `.state`。还需在百度服务商后台确认免费额度和计费限制；客户端不主动开通后付费，并不等于服务商保证免费。

### GitHub 趋势预演

使用 Python 3.12，在 `.env` 中配置模型参数及可选的 `GITHUB_TOKEN`，然后执行：

```sh
DRY_RUN=true python -m ai_daily.github_trends_cli
```

`GITHUB_TRENDS_STATE_PATH` 默认是 `.state/github-trends/baseline.json`，与 AI 摘要状态隔离。首次运行或基线丢失时只建立基线；dry-run 不保存这份基线，也不发送消息。真实趋势需要正式保存基线并经过至少 72 小时，不能用首次预演证明增量报告已经验收。失败后重试按实际观察时长展示增量，不把超过 72 小时的累计增长标成恰好 72 小时。未配置 `GITHUB_TOKEN` 时可能触发匿名请求限额；Actions 使用自己的 `github.token`。

GitHub 缓存只是优化手段，不是永久存储。缓存被清理、过期或恢复失败时，旧内容可能再次入选。可以进入 **Actions** > **Management** > **Caches** 查看缓存，参考 GitHub 的[缓存管理说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manage-caches)，或者执行：

```powershell
gh cache list
```

如果定时任务延迟，可以先手动运行预演检查当前内容；推送有时效要求时，再运行一次正式任务。不要绕过工作流并发控制，同时启动两个正式推送任务。

<a id="source-maintenance"></a>

## 维护内容来源

在项目根目录编辑 [`config/sources.yaml`](config/sources.yaml)。AI 情报只接受百度搜索计划，生产配置不包含 RSS、arXiv、Hugging Face 或 GitHub Releases：

```yaml
baidu_search:
  fixed_queries: []       # 必须恰好 16 条
  rotating_queries: []    # 至少 4 条，每天取 4 条
  first_party_domains: []
  trusted_domains: []
```

配置要求：

- 固定查询覆盖 8 个主题的中英文版本，与 4 个轮换查询合计不超过 20 次。
- 候选只根据 API 返回的标题、链接、站点、片段、时间和分数进行证据判定，不抓取搜索结果页或目标网页。
- `first_party_domains` 用于识别事件主体；没有第一方证据时，只有两个独立 `trusted_domains` 共同支持的事件才可入选。

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

任何搜索请求失败都会使整个 AI 情报工作流失败，不会跳过后继查询或转向其他来源。提交查询或证据域名变更前，应重新执行离线测试和手工 dry-run。

<a id="reference"></a>

## 配置、CLI、工作流和来源参考

<a id="environment-variables"></a>

### 环境变量

CLI 会先加载当前工作目录中的 `.env`，再读取进程环境变量；已有进程环境变量的优先级更高。由于来源文件路径固定为 `config/sources.yaml`，请始终在项目根目录运行命令。

| 变量 | 是否必需 | 默认值 | 限制和作用 |
| --- | --- | --- | --- |
| `BAIDU_SEARCH_API_KEY` | 是 | 无 | 百度官方 Web Search API 凭据，与模型凭据分离。 |
| `AI_API_KEY` | 是 | 无 | 非空的模型服务凭证，以 Bearer Token 形式发送。 |
| `AI_BASE_URL` | 否 | `https://api.teamorouter.cn/v1` | TeamoRouter OpenAI-compatible 基础地址，程序请求 `<base>/chat/completions`。 |
| `AI_MODEL` | 是 | 无 | 唯一生产模型，必须显式填写 `gpt-5.6-luna`；不接受其他模型或自动回退模型。 |
| `DINGTALK_WEBHOOK` | 是 | 无 | 非空 HTTPS URL，可以包含一个非空 `access_token` 查询参数。 |
| `DINGTALK_ACCESS_TOKEN` | 条件必需 | 空 | Webhook 不包含有效 `access_token` 时必需；完整 Webhook 已提供令牌时忽略。 |
| `WINDOW_HOURS` | 否 | `36` | 正整数，百度搜索候选的允许时间窗口。 |
| `MAX_ITEMS` | 否 | `8` | 1 到 8 之间的整数；模型返回超过该数量会校验失败。 |
| `TIMEZONE` | 否 | `Asia/Shanghai` | 报告日期使用的 IANA 时区；未知时区会导致运行失败。 |
| `DRY_RUN` | 否 | `false` | 不区分大小写的 `1`、`true`、`yes` 或 `on` 表示真，其他值表示假。预演模式只打印内容，不发送或保存状态。 |
| `STATE_PATH` | 否 | `.state/sent.json` | 本地已发送状态 JSON 路径；保存时自动创建父目录。 |
| `DELIVERY_STATE_PATH` | 否 | `.state/deliveries.json` | 已成功推送的北京时间日期状态，仅定时任务启用每日一次保护时使用。 |
| `ENFORCE_DAILY_ONCE` | 否 | `false` | 定时任务设为 `true`；当天已经成功推送时记录 `status=already-sent` 并跳过。 |

仓库中的 [`.env.example`](.env.example) 只包含变量名、安全默认值和空凭证字段。真实 `.env` 必须保持未跟踪状态。

### CLI

项目提供以下 AI 情报运行方式，不支持其他命令行参数：

```powershell
python -m ai_daily.cli
ai-digest
```

退出码 `0` 表示运行成功，并对应以下一种状态：

| 状态 | 含义 | 钉钉 | 状态文件 |
| --- | --- | --- | --- |
| `dry-run` | 已生成日报预演分片 | 不调用 | 不修改 |
| `sent` | 钉钉已接受全部 AI 情报分片 | 顺序发送 | 保存入选 URL 和事件；启用每日保护时记录当天成功 |
| `empty` | 最近 36 小时没有合格候选 | 不调用 | 不修改 |
| `already-sent` | 当天定时消息已成功发送，无需重复 | 不调用 | 不修改 |

退出码 `1` 表示配置、分析、推送或文件处理失败。为避免泄漏凭证，涉及请求信息的错误会使用通用描述。

使用同一脱敏固定样本验证已由订阅者选定的唯一生产模型：

```powershell
teamorouter-model-validation run --repetitions 3 --output .state/teamorouter-model-validation.json
```

验证命令先通过实时 `GET /v1/models` 确认 `gpt-5.6-luna` 可用，再用完全相同的固定样本调用三次；不会调用或切换到其他模型。结果文件只包含脱敏样本输出、安全错误分类、token usage 和成本估算，不包含密钥。缺少 `AI_API_KEY` 时退出码为 `2` 且不生成结果文件。自动检查通过只会标记为待人工评审；真实结果、统一人工评分、成本证据和验证门槛见 [`docs/model-evaluation/2026-09-10-teamorouter-production-model.md`](docs/model-evaluation/2026-09-10-teamorouter-production-model.md)。

### 来源行为

AI 情报每天执行 16 个固定查询和 4 个轮换查询，只调用百度官方 `POST /v2/ai_search/web_search` API。程序不解析百度结果页，不请求搜索结果所指的目标网页，也不把 RSS、arXiv、Hugging Face 或 GitHub Releases 用作摘要发现来源。

返回的线索会先按 36 小时窗口、30 天 URL 历史、7 天事件历史、每站最多 3 条和证据等级处理，最多向模型提交 40 条。搜索数据始终视为不可信输入，模型返回的来源和 URL 由程序重新绑定到本次候选。

### GitHub Actions 工作流

| 文件 | 显示名称 | 触发方式 | 作用 |
| --- | --- | --- | --- |
| [`.github/workflows/test.yml`](.github/workflows/test.yml) | `Test` | Push、Pull Request | 安装 Python 3.12 依赖并运行离线测试。 |
| [`.github/workflows/daily.yml`](.github/workflows/daily.yml) | `AI 情报摘要` | 08:30 cron、手动触发 | 调用百度搜索与唯一模型，预演不发送或写状态。 |
| [`.github/workflows/github-trends.yml`](.github/workflows/github-trends.yml) | `GitHub AI 趋势报告` | 08:45 cron、手动触发 | 每日更新独立基线，满 72 小时后生成报告；首次只建基线。 |

工作流的仓库内容权限都是只读，并且第三方 Action 都固定到完整的提交 SHA。AI 情报工作流的手动输入参数为布尔值 `dry_run`，默认值是 `true`。

<a id="architecture-security"></a>

## 架构与安全设计

### 数据流程

```text
sources.yaml + 独立搜索/模型凭据
             |
             v
百度官方 Web Search API（最多 20 次）
             |
             v
时间/站点/URL/事件去重 -> 证据分级 -> 最多 40 条
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

模型只能选择候选证据中存在的候选 ID；标题、来源和 URL 一律由本地候选重新绑定。返回值必须符合严格的日报数据结构：包含 1 到 8 条内容、2 到 3 条趋势、满足字段长度限制，并且不超过 `MAX_ITEMS`。未知或重复候选 ID、非法结构和业务约束违规都会显式失败，不能进入发送接缝。这些限制可以减少虚构内容，但不能证明生成文字一定正确。修改模型或信息源后，应人工检查预演。

钉钉文本字段会进行空白规范化和 Markdown 标点转义，链接目标会做百分号编码。单条消息最多 18,000 个字符，并且只在完整条目之间拆分。当前仅支持未加签的钉钉自定义机器人，不支持要求签名的机器人。

### 密钥边界

- `.env` 和 `.state/` 已被 Git 忽略。
- GitHub Secrets 只进入 CLI 运行步骤，不会传给依赖安装或缓存步骤。
- 两个工作流的 Job Token 都只有仓库内容只读权限；AI 情报运行步骤不读取 GitHub Token。
- 模型和钉钉凭证使用支持隐藏值的配置类型。
- HTTP 依赖日志被限制在 warning 及以上级别。
- 百度搜索错误只记录安全的异常类名和状态，不记录响应正文。
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
    ('^tests/test_dingtalk\.py:\d+:\s+dingtalk_access_' + 'token=access_token,$')
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

不要绕过校验。模型输出无效时工作流失败，钉钉保持静默，也不写入成功状态。应修正接口地址、模型或提示词兼容性，再重新运行预演。

### HTTP 401

模型接口返回 401 或 403 时不会重试模型请求，工作流失败且钉钉保持静默。401 通常表示 `AI_API_KEY`、`AI_BASE_URL` 或服务商授权不正确；403 还可能表示额度、模型权限或服务商访问限制。百度搜索返回 401/403 时应检查 `BAIDU_SEARCH_API_KEY` 和 Web Search 权限。钉钉 HTTP 授权失败会显示通用错误 `DingTalk delivery failed`。

请通过 `.env` 或仓库 Secret 的隐藏输入重新填写凭证，不要打印凭证。疑似泄漏时应轮换密钥，不要把密钥粘贴到 Issue 或日志中。

### HTTP 429 或 5xx

百度搜索请求的单次超时为 20 秒；限额、免费额度耗尽或服务端错误会使本期失败，不会切换搜索来源。模型分析对超时、连接错误、429 和 5xx 使用同一 `gpt-5.6-luna` 最多尝试 3 次，绝不切换模型；持续失败会使工作流失败且钉钉静默。钉钉推送超时为 20 秒，最多尝试 3 次；发送失败不写成功状态。

### 出现 `DingTalk rejected the message` 或非零 `errcode`

这表示钉钉返回了 HTTP 成功，但业务 `errcode` 非零或格式无效。程序会隐藏钉钉响应消息，因为响应可能回显 Webhook 信息。

请在钉钉机器人设置中确认：

- 机器人已启用并且是未加签模式。
- 机器人允许在目标群中使用。
- 使用的是当前有效令牌。
- Webhook 恰好包含一个非空 `access_token`，或者使用基础 Webhook 配合 `DINGTALK_ACCESS_TOKEN`。

然后通过隐藏输入更新仓库 Secrets，先预演，再正式推送。

### 出现 `status=empty`

这表示最近 36 小时没有获取到通过证据规则的合格候选。这是成功但静默的运行：程序不调用钉钉，不回放旧内容，也不记录当日成功状态。

### 出现 `status=already-sent`

当天已有一个定时运行完成了全部钉钉发送，因此再次运行时会在抓取、模型和钉钉调用之前正常跳过。这是每日一次保护的预期行为，不是错误。手动运行不会写入每日成功日期。

### 定时工作流延迟或没有出现

GitHub cron 不是精确调度器，平台负载较高时可能延迟。确认 Actions 已启用，`daily.yml` 位于仓库默认分支，并且唯一 cron 是 `30 0 * * *`。不要把 cron 改成本地时间，因为 GitHub cron 使用 UTC。如果定时运行没有出现，先手动触发 dry-run 并检查预览、退出状态与安全日志。

### 状态文件损坏或旧内容重复出现

删除或修复本地状态文件前，先确认 `STATE_PATH` 和 `DELIVERY_STATE_PATH` 指向正确文件。程序会拒绝包含原始 URL、无效哈希、无效日期或不带时区时间戳的状态文件。在 Actions 中，缓存未命中或被清理可能使旧内容重新符合条件。请检查 **Actions** > **Caches**，先运行预演，并且不要把缓存当作永久存储。

## 许可证

当前没有声明许可证。在仓库所有者添加许可证前，应将本项目视为私有、保留所有权利的代码。
