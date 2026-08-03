# 浏览器上传与多账号上传

浏览器上传使用 Playwright 连接带 `--remote-debugging-port=9222` 的 Chrome，并复用 YouTube Studio 登录态。macOS 下正常入口是 GUI 的“批量与上传”区域。

## GUI 频道方案编辑器

在 GUI 的“批量与上传”区域点击“编辑频道上传方案”，可以不用手写 JSON，直接维护每个 YouTube 频道/账号：

- 左侧是频道列表，顺序就是多账号上传顺序。
- 每个频道可以单独设置 `Chrome 资料`，对应独立登录态。已有频道 ID 时按 ID
  强制校验；旧方案尚未绑定 ID 时才临时按频道名称校验。无法确认时程序会停止
  上传，绝不会沿用浏览器上次停留的频道。
- 点击“打开登录并绑定当前 YouTube 频道”后，程序会从 Studio 地址读取并
  保存唯一的 `youtube_channel_id`。上传时直接进入该 ID 对应的 Studio 页面并
  校验 ID；频道显示名称只用于界面辨认，即使频道改名也不会串号。
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

## 定时发布测试

在 GUI 的“批量与上传”区域点击“编辑频道上传方案”，再打开右侧的“油管内定时”页签。当前测试版只用于一个已经完成的视频：

1. 在任务列表中只选择一个已完成任务。
2. 开启“启用定时发布”。
3. 分别填写年、月、日、时、分，所有输入框只需要数字；年份默认跟随电脑日期，时间不需要输入冒号。
4. 点击“测试定时上传选中视频”。
5. 程序进入 YouTube Studio 公开范围页后，展开“安排时间”，点击日期按钮，在日历顶部输入框全选并填写 `2026年7月23日` 格式（备用格式为 `2026/07/23`）；随后在时间框全选并填写 `17:30` 格式。
6. 程序校验页面显示的日期和时间并点击“预定”。如果出现“我们仍在检查你的内容”提示，会先点击“知道了”。
7. 从预约成功弹窗提取视频链接并点击“关闭”。

也可以在主界面的任务列表中右键展开“定时任务”，再选择“油管内定时”或“脚本内定时”。批量窗口左侧显示每个文件名，右侧分别编辑年、月、日、时、分，并提供“同步所有年份”“同步所有月份”和“从第一个视频开始每24小时排列”。任务已经上传或已有定时记录时，会先提醒是否可能重复上传；用户确认“重新发布定时任务”后仍可继续安排。

定时窗口的“目标 YouTube 频道”直接列出当前配置中“编辑频道上传方案”保存的频道名称；旁边的 Chrome 资料为只读显示，始终由所选频道方案带出，不能在定时窗口单独改写。

启用测试开关后，“生成后自动上传”会被安全跳过，避免尚未实现批量排期时多个任务使用同一个发布时间。关闭测试开关后，原有立即发布流程不变。

如果日期或时间输入后页面没有显示目标值，程序会在点击“预定”前停止。此时查看任务日志，不要直接重试上传；先确认 Studio 中是否已经存在草稿或预约视频。

## 三种互斥发布方式

每个频道方案只能选择一种发布方式：

- `直接发布`：视频制作完成后立即上传。
- `油管内定时`：立即上传到 YouTube，再由 YouTube 到指定时间公开。
- `脚本内定时`：成片先保留在本地，到达队列时间后才开始上传。

在任一页签中启用另一种方式时，GUI 会先要求确认。确认切换后，另外两种方式中会冲突的输入框和按钮会被锁定。

## 脚本内定时

“脚本内定时”按频道保存首次发布日期、每天发布时间、发布间隔、时区、错过时间和视频未完成时的处理方式。待发布队列保存在 `data/script_publish_queue.json`，软件重启后会继续读取。

队列会根据来源文件名识别 `第1话/第2话`、`第一期/第二期`、`第1集/第2集`、`EP01/EP02` 以及上中下篇。同系列按集数连续排列；没有明确集数标记的文件按独立故事处理。点击“重新识别并计算队列”可以重新扫描现有任务并预览排期，也可以上移、下移或手动修改系列和集数。

脚本内定时要求 GUI 持续运行、Mac 不进入深度睡眠、网络正常且频道 Chrome 登录态有效。上传失败后会在一小时后自动重试。

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
    "title_template": "{candidate_title} #小说 #推文",
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
    "title_template": "{candidate_title}",
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
| `youtube_channel_id` | 已绑定的 YouTube 唯一频道 ID（`UC...`） |
| `youtube_channel_name` | 绑定时读取的频道显示名称 |
| `flow` | `simple` 或 `full` |
| `visibility` | 发布公开范围 |
| `upload_policy` | YouTube Studio 里的上传政策名 |
| `ad_interval` | 中贴广告间隔秒数 |
| `ad_start` | 首个广告起始秒数 |
| `title_template` | 上传标题模板 |
| `description` | 上传说明模板 |
| `ad_suitability_template` | 广告分级问卷模板 |

标题模板可用：`{candidate_title}`、`{short_title}`、`{clean_title}`、`{title}`、`{intro}`、`{author}`、`{tags}`、`{job_id}`。其中 `{candidate_title}` 和 `{short_title}` 都是本次随机选中的候选标题；`{tags}` 只读取 GUI 的“模板 Tags”。程序先完整执行模板，再用内容标签填充 100 字上限内的剩余空间，并跳过模板中已有的标签。脚本绝不会自动追加 `【朗読・小説】`；如需该文字，请手动写入模板。

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
