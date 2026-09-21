# 本地实施与验收记录（2026-09-20）

## 结论

01—07 已集成本地 master（HEAD `8c6c5fc`）；08 清理未提交，整体验收仍未完成。每日搜索用量账本已按用户许可加入工作区，但不得启用生产定时任务。当前环境存在 `/Users/moweicuryy_1/.agents/skills/implement/SKILL.md`；08 按用户例外不调用该技能、不创建提交。

## 票据、分支与提交

| 票据 | 独立实施分支 | 原票据提交 | 主分支集成提交 | 当前可核实的 Worktree |
| --- | --- | --- | --- | --- |
| 01 | codex/ticket-01-app-seams | 44db53e | 810cf03 | 历史目录未留存 |
| 02 | codex/ticket-02-baidu-search-dry-run | f2b1f2d | 39c40ca | 历史目录未留存 |
| 03 | codex/ticket-03-evidence-ai-digest | c3ce2f4 | f6705ed | 历史目录未留存 |
| 04 | codex/ticket-04-qianfan-production-model | b0613af | 4adc305 | /private/tmp/dingtalk-ticket-04（Git登记仍在，目录已不存在） |
| 05 | codex/ticket-05-ai-digest-workflow | 0856380 | 8c6c5fc | /private/tmp/dingtalk-ticket-05（Git登记仍在，目录已不存在） |
| 06 | codex/ticket-06-github-trend-baseline | 495f0a0 | 873b2ec | 历史目录未留存 |
| 07 | codex/ticket-07-github-72h-report | b842945 | 43d2c23 | /private/tmp/dingtalk-ticket-07（Git登记仍在，目录已不存在） |
| 08 | 不创建独立分支；主工作区 | 无 | 无 | 项目主目录，改动未暂存、未提交 |

04 分支名为历史命名，实际模型为订阅者选择的 TeamoRouter `gpt-5.6-luna`，不是千帆模型。01—07 历史 Agent ID 与逐批并发时序无法从当前可用记录恢复，不虚构对应关系。Git 提交可核实的集成顺序是 01→02→06→03→04→07→05；满足显式依赖顺序，但提交顺序不能证明历史派发是否并行。

本轮只有主 Agent 写入08；两个 `/code-review` 子 Agent 并行只读：`/root/ticket08_standards_review`、`/root/ticket08_spec_review`。没有并行写入同一目录，也未清理遗留 Worktree 登记。

## 依赖与验收证据

依赖：01→02；02→03、04；03+04→05；01→06；04+06→07；05+07→08。08 当前存在整体规格与真实验收缺口，不能标记完成。

| 票据 | 当前证据与范围 |
| --- | --- |
| 01 | 两个应用入口、独立运行结果和可注入边界；`tests/test_application_seams.py`、两类应用入口测试。旧链路行为测试已随08删除，不能用当前199项证明历史旧行为。 |
| 02 | `tests/test_baidu_digest.py` 覆盖百度协议、预演、缺凭据、畸形响应和安全失败；不抓网页。 |
| 03 | 同上覆盖16+4查询、36小时、去重、站点/候选/条目限额、证据绑定；每日累计配额已由独立账本执行。 |
| 04 | `docs/model-evaluation/2026-09-10-teamorouter-production-model.md` 保存此前真实固定样本三轮验证记录；本轮没有重复付费模型调用。估算费用不是供应商账单硬上限。 |
| 05 | 工作流及应用测试覆盖调度、dry-run、失败静默、成功后保存和旧URL状态。2026-09-20两次真实AI dry-run均返回empty，没有生成摘要或验证当日生成质量。 |
| 06 | `tests/test_github_trends.py` 覆盖首次基线、200候选上限、过滤、分页和失败保留状态。真实基线 dry-run 已成功建立临时结果。 |
| 07 | 同上覆盖72小时边界、3+2配额、候选不足、延迟后实际时长、发送失败和状态隔离。真实72小时趋势未验证。 |
| 08 | 已移除旧采集器、选择器、占位/错误通知及专属依赖和测试；配置只接受百度发现入口；新增独立百度每日用量账本并接入 Actions 缓存；README更新双报告边界和已知阻塞。未提交。 |

## 当前离线验证

使用 Python 3.12 环境并显式指定当前源码，避免临时虚拟环境的旧 editable 路径：

```sh
PYTHONPATH="$PWD/src" /private/tmp/codex-ticket05-venv/bin/python -m pytest -o addopts= -q
git diff --check
```

结果：**200 passed in 5.61s**；差异格式检查无错误。原 pyenv/临时安装路径问题与代码回归分开处理，未通过修改业务逻辑掩盖环境故障。GitHub Actions 仅本地YAML/测试检查，未运行远端CI。

凭据检查：`.env` 被 Git 忽略；对65个现有源码、测试、配置、文档和票据文件进行本地实际凭据精确匹配扫描，0匹配；已跟踪文件的常见密钥/Webhook长令牌模式扫描无命中。仅输出文件名/计数，不输出凭据。扫描不构成所有历史日志、Git历史或供应商端无泄漏的证明。未暂存 `.neuralmemory`、`.env` 或其他用户改动。

## Standards

固定点 `8c6c5fc`，审查未提交的08差异（不能用三点提交差异冒充未提交审查）。0硬规范违规。冗余非Optional字段None检查已修复并全量复测。跨文件清理为票08明确范围，保留为1项非阻塞判断，不扩大重构。

## Spec

2项验收缺口，保持分别记录：

1. GitHub真实基线 dry-run 已成功：使用本地 Token 只读调用官方 API，得到 `status=dry-run`、84 个有效候选、103 次官方 API 请求；临时状态未写入。代码同时修复了分页总数变化导致的误判。
2. 当前AI真实dry-run仅空结果，没有本次真实内容生成质量证据；历史04固定样本验证仍保留，不能混称当日端到端验证。

旧来源清理与流程隔离符合；`first_party_domains` 只用于百度线索证据分级，不是恢复旧直采入口。

两轴总结：Standards 0硬违规、1范围内判断；Spec 2项未完成验收。没有把其中一轴通过当作另一轴通过。

## 真实调用与阻塞

- AI：本日两次真实运行均为 `status=empty candidates=0 selected=0 parts=0`；无消息、无成功状态，未调用模型。两次使用隔离临时用量目录，未写生产状态文件。
- GitHub：修复分页稳定性后真实应用预演 `status=dry-run repositories=84 parts=0 failure_type=None`，103次官方 API 请求，临时基线文件不存在；只读、未发送、未写正式状态。
- `GITHUB_TOKEN` 已配置于本地 `.env`，未输出或写入仓库；本次使用的是官方 GitHub API。
- 用户已允许 dry-run 和失败运行保存独立用量记录；它不写日报成功状态。服务商侧限额仍必要，否则缓存丢失、不同执行环境无法获得全局费用保证。

## 未执行与保留项

没有 push、PR、部署、远端Issue关闭、真实钉钉发送、生产启用。08 不创建提交。未修改或恢复用户原有的旧设计文档删除，也未清理 `.neuralmemory`。待通过 AI 非空内容质量验证并在 Actions 缓存环境验证配额恢复后，才能解除整体上线阻塞。
