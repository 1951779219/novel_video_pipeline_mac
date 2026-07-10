# 反馈与自动更新闭环

这个项目现在沿用短剧流水线的三仓库结构：

| 仓库 | 可见性 | 用途 |
| --- | --- | --- |
| `1951779219/novel_video_pipeline` | Private | 源码、发布 workflow |
| `1951779219/novel_video_pipeline_mac_release` | Public | `latest.json` 和更新包 |
| `1951779219/novel_video_pipeline_feedback` | Public | 用户提交 BUG、建议和问题 |

用户反馈入口：

```text
https://github.com/1951779219/novel_video_pipeline_feedback/issues/new/choose
```

公开反馈仓库包含：

```text
.github/ISSUE_TEMPLATE/bug_report.yml
.github/ISSUE_TEMPLATE/feature_request.yml
.github/ISSUE_TEMPLATE/config.yml
.github/workflows/issue_triage.yml
tools/github_issue_triage.py
```

Issue 创建、编辑、重开时会自动打：

- `type:bug` / `type:feature` / `type:question`
- `priority:P0` 到 `priority:P3`
- `needs:repro`
- `area:scraper` / `area:tts` / `area:image` / `area:video` / `area:upload` / `area:update` / `area:ui`

每日巡检：

```text
.github/workflows/feedback_daily.yml
tools/feedback_daily.py
```

每天 09:00（Asia/Shanghai）会扫描公开反馈仓库里的 open issues，重点看最近更新、未分诊、需复现、P0/P1 的问题，并在私有源码仓库生成或更新当天的 `[反馈巡检] YYYY-MM-DD` 报告。报告按“分诊智能体 / 诊断智能体 / 修复智能体 / 发布智能体”整理：

- 新增或更新的问题
- 优先级和影响模块
- 可以自动修复的候选项
- 需要用户补充复现信息的问题
- 是否应该 bump 版本并发布更新

闭环流程：

1. 用户在软件里点“提交反馈”，打开公开反馈仓库。
2. 用户提交 issue。
3. GitHub Actions 自动分类、标优先级和模块。
4. 每日巡检 workflow 生成维护报告。
5. Codex 自动维护任务读取反馈和报告，多智能体分析，能安全修复时提交改动。
6. 修复后修改 `app/version.py`，运行验证。
7. 运行 `scripts/publish_github_release.sh`，生成脱敏更新包和 `latest.json` 并上传 GitHub Release。
8. 软件读取 `latest.json`，用户在“软件更新”页下载并应用。macOS 更新器会保护 `data/settings.json`、`data/profiles/`、任务和浏览器登录态。

具体发布步骤见 `docs/online_update_github.md`。

自动修复护栏：

- 可以自动做：文案、配置默认值、更新检查、无副作用的小型 Python 修复、测试或脚本修复。
- 必须先确认：删除用户数据、批量移动文件、账号/令牌/上传流程、付费 API 消耗、改变生成结果的大范围逻辑。
- 每次修复必须至少运行相关 `py_compile` 或更高等级验证。
- 只有验证通过的修复才允许打 tag 触发在线更新。
