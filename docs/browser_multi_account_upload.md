# 浏览器上传与多账号上传

浏览器上传使用 Playwright 连接带 `--remote-debugging-port=9222` 的 Chrome，并复用 YouTube Studio 登录态。macOS 下正常入口是 GUI 的“批量与上传”区域。

## GUI 频道方案编辑器

在 GUI 的“批量与上传”区域点击“编辑频道上传方案”，可以不用手写 JSON，直接维护每个 YouTube 频道/账号：

- 左侧是频道列表，顺序就是多账号上传顺序。
- 每个频道可以单独设置 `Chrome 资料`，对应独立登录态。
- `上传流程` 可选精简或完整。精简跳过创收、广告位和分级；完整会继续走上传政策、创收、广告位和广告分级。
- `公开范围`、`上传政策`、`广告位`、`标题模板`、`说明模板` 和 `广告分级模板` 都可以按频道单独设置。
- “设为当前单频道上传”会关闭多账号顺序上传，只用选中的频道。
- “顺序上传全部启用频道”会打开多账号模式，按列表顺序上传所有启用频道。
- “打开此频道 Chrome 登录”会用该频道的 Chrome 资料启动 YouTube Studio，方便首次登录或检查账号。

底部的“高级: 方案 JSON”仍然保留，供 agent 批量修改或排查兼容问题。

## 单账号上传

最少需要设置：

- `upload_enabled`: 开启生成后自动上传。
- `browser_active_profile`: 当前上传方案名。
- `browser_chrome_profile`: Chrome 调试资料名，默认 `Default`。
- `youtube_visibility`: `PRIVATE` / `UNLISTED` / `PUBLIC`。
- `browser_flow`: `simple` 或 `full`。

`simple` 会尽量走无创收精简流程；`full` 会继续处理权利管理、创收、广告位和广告分级。

## 多账号上传的工作方式

打开 GUI 里的“上传全部启用方案”后，程序会按 `browser_profiles` JSON 的顺序逐个上传同一个工程的最终视频。

每个方案可以绑定一个独立的 `chrome_profile`。程序会把它映射到：

```text
data/chrome_debug_profiles/<chrome_profile>
```

每个资料目录都需要第一次手动登录对应的 YouTube 账号。登录完成后再次上传会复用这个资料目录。

为了避免多个账号抢同一个 Chrome 调试端口，多账号模式是顺序执行，不并发上传。

## 上传方案 JSON 示例

GUI 频道编辑器保存后，会同步写入“高级: 方案 JSON”。也可以直接在 JSON 中放：

```json
[
  {
    "name": "账号A-精简",
    "enabled": true,
    "chrome_profile": "youtube_account_a",
    "flow": "simple",
    "visibility": "PUBLIC",
    "upload_policy": "BTRA",
    "ad_interval": 60,
    "ad_start": 0,
    "title_template": "《{clean_title}》{intro} #小说 #推文",
    "description": "每天更新高分小说推文\\n{title}",
    "ad_suitability_template": ""
  },
  {
    "name": "账号B-完整创收",
    "enabled": true,
    "chrome_profile": "youtube_account_b",
    "flow": "full",
    "visibility": "PUBLIC",
    "upload_policy": "BTRA",
    "ad_interval": 60,
    "ad_start": 0,
    "title_template": "一口气看完《{clean_title}》{short_title}",
    "description": "",
    "ad_suitability_template": {
      "default": 1,
      "questions": {
        "暴力": 1,
        "成人": 1,
        "不当语言": 1
      }
    }
  }
]
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `name` | 上传方案名，也会显示在日志里 |
| `enabled` | 多账号模式下是否执行 |
| `chrome_profile` | 独立 Chrome 调试资料目录名 |
| `flow` | `simple` 或 `full` |
| `visibility` | 发布公开范围 |
| `upload_policy` | YouTube Studio 里的上传政策名 |
| `ad_interval` | 中贴广告间隔秒数 |
| `ad_start` | 首个广告起始秒数 |
| `title_template` | 上传标题模板 |
| `description` | 上传说明模板 |
| `ad_suitability_template` | 广告分级问卷模板 |

标题模板可用：`{clean_title}`、`{title}`、`{short_title}`、`{intro}`、`{author}`、`{tags}`、`{job_id}`。

## 输出结果

上传完成后，任务目录会写入 `upload_result.json`。单账号和多账号都包含第一条上传结果，另外多账号会有 `uploads` 列表：

```json
{
  "profile": "账号A-精简",
  "chrome_profile": "youtube_account_a",
  "video_id": "xxxxx",
  "url": "https://www.youtube.com/watch?v=xxxxx",
  "uploads": [
    {
      "profile": "账号A-精简",
      "chrome_profile": "youtube_account_a",
      "video_id": "xxxxx",
      "url": "https://www.youtube.com/watch?v=xxxxx"
    }
  ]
}
```

主界面仍显示第一条 URL，完整列表看 `upload_result.json`。

## 首次登录步骤

1. 在 GUI 里配置第一个 `chrome_profile`，比如 `youtube_account_a`。
2. 开启上传并运行一次任务。
3. Chrome 被打开后，如果 YouTube Studio 未登录，就手动登录。
4. 登录成功后重跑上传。
5. 对第二个账号重复以上步骤，使用不同的 `chrome_profile`。

## 排错

如果一直提示连接不到 Chrome 调试端口，运行：

```bash
scripts/start_chrome_debug_macos.command
```

如果上传到错误账号，检查对应方案的 `chrome_profile` 是否复用了另一个账号的资料目录。

如果多账号上传中途失败，已经成功的账号会保留在 `upload_result.json`，修好登录或政策设置后可以重新执行上传阶段。

如果 YouTube Studio 页面卡住，上传脚本会按配置自动重启 Chrome 并重试。相关参数是 `browser_auto_restart` 和 `browser_stall_timeout_min`。
