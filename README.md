# 小说推文长视频流水线

把小说/推文文本变成长视频：内容抓取 -> 整理/洗稿切片 -> TTS -> 分镜出图 -> 字幕 -> 竖屏视频 -> 可选上传 YouTube。

当前版本重点增强了三件事：

- 国内内容来源：内置晴天/番茄聚合接口，支持番茄、七猫、书旗、塔读、QQ 阅读等聚合来源搜索与抓取。
- 阅读/Legado 兼容：提供 `legado_static` 轻量解析器，可导入普通静态 JSON 书源；复杂 `<js>` 书源由专用聚合引擎处理。
- 长视频稳定性：后台任务、`status.json`、`log.txt`、已有音频/图片复用、长视频 ffmpeg 单次图片清单合成、内嵌 ASS + 外置 SRT 字幕。
- 人设与剧情图提示词：导入 v1.0.18 的人设分析、视觉主题锁、剧情图人物锁和可选参考图链路；生成任务会写入 `character_profiles.json`、`character_reference_manifest.json` 和带 `character_context` / `theme_context` 的 `prompts.json`。
- 视频生图解决方案1：保留“每张图秒数”控制的图片预算，不增加出图数量；先分析全篇稳定背景，再从每个旁白预算窗口选择一个完整高光事件生图，并可按高光所在旁白位置调整换图时间。

## 文档入口

- 模块和提示词修改教程：`docs/module_prompt_editing_guide.md`
- 浏览器多账号上传：`docs/browser_multi_account_upload.md`
- GitHub 线上更新发布：`docs/online_update_github.md`
- 分包机器 agent 升级说明：`docs/agent_upgrade_playbook.md`
- Agent 快速索引：`AGENTS.md`
- 提示词示例：`docs/tweet_prompt_templates.md`

## 启动

macOS 首次使用：

```bash
cd "/Users/yang/Documents/novel video pipelin"
scripts/setup_macos.sh
```

之后可双击或运行：

```bash
scripts/start_gui_macos.command
./Open_GUI.command
```

复制到其他 Mac 使用时，建议先运行 `scripts/setup_macos.sh`，之后固定双击 `Open_GUI.command`。应用默认启动桌面 GUI，不需要 WebUI 常驻进程。

双击：

```bat
启动.bat
```

或在项目目录运行桌面 GUI：

```bat
python -m app.gui
```

生产使用建议启动桌面 GUI：

```bat
桌面GUI.bat
```

桌面 GUI 面向工作人员批量生产：支持番茄搜索加入任务、粘贴文章、导入多个 TXT、查看每个任务实时日志、保存 TTS/图片/节奏/字幕/上传配置、启动多个待处理任务。左侧“任务分类”页可以选择已导入任务的文本来源根目录，按目录及子目录筛选任务，并按名称自然顺序、添加日期、原文修改日期、任务更新时间或制作阶段排序；切回书库/文件导入页会恢复全部任务的默认顺序。任务仍然走独立 worker 进程和 manifest 续跑机制。

如果电脑开了系统代理，启动脚本会自动设置 `NO_PROXY=localhost,127.0.0.1,::1`，避免本地接口调用被代理拦截。
macOS 启动脚本还会把 `/opt/homebrew/bin`、`/usr/local/bin` 和 `~/.local/bin` 加进 PATH，方便从 Finder/双击启动时找到 `ffmpeg`、`ffprobe` 和本地 AI 服务命令。

### macOS 26/Tahoe Tkinter 启动错误

如果双击后出现：

```text
macOS 26 (...) or later required, have instead 16 (...) !
zsh: abort
```

这是当前 Python/Tk GUI 运行时不兼容，不是小说流水线任务本身失败。启动脚本会自动检测并尝试重建 `.venv`。如果仍失败，请在那台 Mac 上安装带 Tk 支持的 Python 后再双击：

```bash
./Install_Mac_Dependencies.command
```

如果安装器提示没有 Homebrew，先安装 Homebrew：

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

新版 `Install_Mac_Dependencies.command` 也可以在没有 Homebrew 时直接询问并运行官方 Homebrew 安装器。Homebrew 安装完成后，它会继续安装 GUI 需要的 Python/Tk/FFmpeg。也可以手动运行：

```bash
brew install python@3.12 python-tk@3.12 ffmpeg-full
```

也可以指定 Python.org 安装版：

```bash
export NOVEL_VIDEO_PYTHON=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3
./Open_GUI.command
```

## 人设与剧情图提示词

GUI 的“API 设置与 Key”区域集中保存统一接口、TTS、分镜、图片、人设图、剧情参考图和封面的 Provider、Base URL、API Key 与模型；“图片与提示词”区域保留业务设置和模型输入副本：

- `统一 API 接管文本和全部图片`：Base URL/API Key 只填一次，文本模型和图片模型分别选择；分镜、洗稿、人设、剧情图、封面默认继承。
- 所有 API Key 输入框默认用黑点遮挡，右侧小眼睛可临时显示/隐藏，并支持一键复制。
- 同一个模型如果在“API 设置与 Key”和原业务分组中填写不同，保存时会提示选择最终采用哪一个，并自动同步两处。
- `AI 人设分析`：从文章里提取主角、配角、视觉主题和固定外观规则。
- `生成人设参考图`：可选，为主角/重要配角先生成全身参考图。
- `剧情图注入视觉主题` / `剧情图注入人物锁`：把统一画风和人物外观锁拼进每张剧情图提示词。
- `剧情图使用人设参考图`：OpenAI 兼容接口会走 `/images/edits`，SD WebUI 会走 `img2img`；其他后端自动退回普通文生图。

## Seedance 无限画布短视频钩子

macOS 可双击或运行：

```bash
scripts/start_seedance_canvas_macos.command
```

打开：

```text
http://127.0.0.1:7871
```

页面里粘贴文案，填写 Seedance API Key、Base URL、模型名、比例和秒数后，可先“生成方案”，再“生成短视频”。默认使用火山方舟/Ark 风格接口：

```text
POST {base_url}/contents/generations/tasks
GET  {base_url}/contents/generations/tasks/{task_id}
```

如果不想在页面里填 Key，也可以在启动前设置：

```bash
export SEEDANCE_API_KEY="你的 key"
export SEEDANCE_MODEL="doubao-seedance-2-0-fast-260128"
export SEEDANCE_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"
```

每次任务会写入：

```text
data/seedance_canvas/jobs/{job_id}/
  script.txt
  seedance_prompt.txt
  plan.json
  tweet_hook.json
  result.json
  *.mp4
```

画布操作：

- 拖动卡片标题栏可移动节点，空白处拖动可平移画布，滚轮可缩放。
- 节点位置保存在浏览器本地，刷新页面后仍会保留。

### AI 转提示词、人设图和九宫图

页面支持两类额外 API：

- 语言 AI：OpenAI 兼容 `/chat/completions` 或 Claude `/messages`，用于把文章转成 `video_prompt`、人设图提示词和九宫图分镜提示词。
- 图片模型：OpenAI 兼容 `/images/generations` 中转站，或本地占位图模式，用于生成角色人设图和九宫图素材。

Key 不会写入 `data/seedance_canvas/settings.json`；也可以用环境变量：

```bash
export PROMPT_LLM_API_KEY="语言 AI key"
export IMAGE_API_KEY="图片模型 key"
```

接口：

```text
POST http://127.0.0.1:7871/api/ai_prompts
POST http://127.0.0.1:7871/api/reference_images
```

`/api/reference_images` 支持：

```json
{
  "title": "可空标题",
  "script": "文章或剧情文案",
  "mode": "character",
  "image_provider": "custom",
  "image_base_url": "https://你的中转站/v1",
  "image_model": "图片模型名",
  "image_api_key": "可空，留空读 IMAGE_API_KEY"
}
```

`mode` 可填 `character`、`grid` 或 `all`。完成后会在对应 job 目录写入：

```text
prompt_bundle.json
character_01.png ...
grid_01.png ... grid_09.png
nine_grid.jpg
reference_images.json
```

给推文流水线接入的入口是：

```text
POST http://127.0.0.1:7871/api/tweet_hook
Content-Type: application/json

{
  "title": "可空标题",
  "script": "推文开头或小说剧情文案",
  "api_key": "可空，留空读 SEEDANCE_API_KEY",
  "duration": 5,
  "ratio": "9:16",
  "resolution": "720p"
}
```

服务会返回 `job_id`，任务完成后把同一份 manifest 投递到：

```text
data/tweet_hooks/inbox/{job_id}.json
```

推文流水线只需要读取这个 JSON：`hook` / `tweet_thread` 用作开头钩子，`artifacts.video` 用作短视频钩子素材。想先验证不消耗额度，可传 `"dry_run": true`，它只生成 prompt 和 manifest，不调用 Seedance。

## 内容来源

### 聚合书源

默认来源引擎是 `qingtian`。搜索框支持：

```text
十日终焉
x:十日终焉@番茄
t:十日终焉@七猫
m:十日终焉@番茄
d:短剧名@番茄
```

搜索后复制或选择 `qingtian://...` 引用，完整流水线可直接使用。

内置线路来自用户给的阅读书源结构，默认会优先尝试：

- `https://v1.gyks.cf`
- `http://219.154.201.122:5006`
- `https://api.langge.cf`

不会绕过登录、验证码、禁止访问或反爬限制；如果某线路不可用，会切换备用线路。

### Legado 静态书源

`legado_static` 支持普通 CSS/JSON 规则书源，例如包含：

- `bookSourceName`
- `bookSourceUrl`
- `searchUrl`
- `ruleSearch`
- `ruleBookInfo`
- `ruleToc`
- `ruleContent`

在“配置 -> 抓取来源 -> 普通 Legado JSON 文件路径或 URL”里填入路径或 URL。

复杂 `<js>` 书源不能用通用静态解析器直接执行，应使用 `qingtian` 或后续新增的专用适配器。

## 长视频建议

制作十几小时到四十小时以上的视频时，建议保持：

- `长视频稳定模式`: 开启
- `内嵌字幕`: 开启
- `同时导出 SRT`: 开启
- `短视频 Ken Burns 动效`: 关闭或让长视频模式自动关闭
- `TTS 持续重试直到成功`: 开启
- `TTS 波形校验`: 开启
- `TTS 卡段提示秒`: 建议 240-360，单段卡住会写日志并由子进程超时重试
- `TTS 子进程隔离`: 开启，单段超时会杀掉独立合成进程并重试

长视频模式不会为每张图生成单独 mp4 片段，而是用 concat 图片清单一次性合成，减少临时文件数量和崩溃概率。

## 导入已生成的 MP3（跳过 TTS）

桌面 GUI 的输入区提供两种入口：

- `粘贴文本+MP3`：先在文本框粘贴生成该音频时使用的原文，再选择完整 MP3。
- `导入正文 TXT + MP3（跳过TTS）`：在同一个文件窗口中同时选中 1 份原文 TXT 和 1 份完整 MP3（macOS 可按住 Command 再点击两个文件）。

这类任务会保留原文，跳过标题清理、AI 洗稿和 TTS，直接继续画面规划、生图、字幕、封面和视频合成。MP3 会复制到任务目录，之后移动或删除原 MP3 不会影响续跑。任务列表的“音频来源”会显示“导入 MP3”。
导入 MP3 的任务不会附加日语读音词典，因为它不再调用 TTS。

导入单条完整 MP3 时没有逐句时间戳，因此程序会按每个文本切片的长度估算字幕和换图时间，并保证总时长与 MP3 严格一致。若朗读速度起伏很大，局部字幕可能会有偏差。估算结果保存在 `imported_audio_timing.json`。

## 多文件夹导入

点击“选择文件夹导入（可多选）…”后，macOS 文件夹选择窗口支持一次选择多个小说文件夹。程序会先递归扫描所选文件夹，但不会立即创建任务；确认框会列出实际导入的文件夹数量、扫描到的 TXT/MP3 数量，以及将创建的正文任务数量。选择“否”即可取消整次导入。

如果同时选择了上级文件夹和它的下级文件夹，程序会忽略重复的下级选择，避免同一批文件被导入两次。

## 小说项目与系列共享

导入同目录下名称一致的分集正文时，例如 `作品_01.txt`、`作品_02.txt`，GUI 会在创建任务前询问是否建立小说项目。以后单独导入同名新分集时，也会建议加入已有项目。选择独立导入不会改变原来的单篇任务流程。

“小说项目”页可以按项目筛选任务、将选中任务加入或移出项目、打开项目资料目录，并可按需迁移旧版系列任务。删除或移出任务不会删除项目共享资料。

项目资料保存在：

```text
data/projects/{project_id}/
  project.json
  character_profiles.json
  character_relationships.json
  name_registry.json
  visual_bible.json
  character_reference_manifest.json
  characters/
```

项目任务会共享人物分析、人物姓名与别名、人物关系、视觉主题和人设参考图。每个任务仍保留当次使用的 `character_profiles.json` 与 `character_reference_manifest.json` 快照，方便续跑和排错。标记为 `confirmed` 的人物记录不会被后续自动分析覆盖；新人物会追加到项目人物库。

### 项目添加2：导入前锁定统一小说名

如果正文尚未导入，但统一小说名已经确定，可以先进入“小说项目”页：

1. 点击“新建系列项目”。
2. 填写内部项目名称和对外使用的统一小说名。
3. 保持“锁定，禁止 AI 改名”开启。
4. 根据需要决定是否允许 AI 生成每集标题和封面文案。
5. 设置集数起点、集数格式、上传标题格式和封面系列标记。
6. 点击“导入正文到此项目…”选择 TXT、同名 MP3、词典或文件夹。

定向导入不会再询问项目归属。带 `_01`、`第2集` 等编号的文件优先使用文件编号；没有编号的正文从“集数起点”开始依次编号。

“锁定，禁止 AI 改名”开启后，统一小说名会直接进入各集标题、封面和上传信息，流水线会跳过系列短名称的 AI 生成。关闭“允许 AI 生成每集标题”后，每集标题改用正文确定性生成的本地保底标题，不调用标题模型。关闭“允许 AI 生成封面文案”后，封面只使用人工系列标记，不采用 AI 每集文案。

格式字段支持：

```text
{series_title}   统一小说名
{episode}        集数
{total}          当前已知总集数
{episode_label}  格式化后的集数标签
{ai_title}       AI 每集标题或本地保底标题
```

本次变更的本地回滚名称为“项目添加2”。对应修改前快照保存在 `data/backups/code_changes/项目添加2_20260730_175825/`，双击其中的恢复脚本可以恢复程序文件，不会删除任务、项目和用户设置。

## 日语 Edge / VOICEVOX TTS 读音词典

桌面 GUI 的输入区域可以选择一份日语读音词典 TXT。选中词典后，新加入的粘贴文本、TXT 文件或小说搜索任务会自动附加该词典。若任务队列中已有选中的任务，选择词典时会立即附加；启动任务前还会再次检查当前词典。任务列表的“读音词典”列会显示“已附加 N条”，不要在显示“未附加”时开始长任务。

词典使用 UTF-8 文本，一行一条，半角 `=` 和全角 `＝` 都支持：

```text
# 人名、地名和专有名词
三国志演義=さんごくしえんぎ
黄巾の乱=こうきんのらん
秦の始皇帝=しんのしこうてい
始皇帝=しこうてい
```

空行以及以 `#`、`//` 开头的注释行会被忽略。同一个词不能在同一份词典中指定两个不同读音。匹配采用最长词优先，因此“秦の始皇帝”不会先被较短的“始皇帝”替换。

手动上传的词典会在 Provider 为 `edge` 或 `voicevox` 时修改送入 TTS 的临时朗读文本。`segments.json`、字幕、标题、图片提示词和正文仍保留原来的汉字。任务内部会保存词典副本以及 `audio/tts_pronunciation_preview.json`，方便核对实际替换结果；它们不会写入最终视频或上传内容。给已有任务更换词典时，GUI 会重置其旧 TTS 段和受音频时长影响的下游缓存。

### AI 洗稿、朗读净化与自动读音

GUI 的“AI 洗稿改写”控制是否调用文字模型改写正文。提示词和批次大小可在界面中设置。

“TTS 朗读净化”是独立开关，不属于洗稿，关闭洗稿时也可以单独使用。Edge 会清理容易被念成“ダッシュ”等词的连续破折号，并统一部分装饰标点；VOICEVOX 则保留作者原有的句号、逗号、顿号、问号、感叹号、引号、省略号、破折号、波浪线和换行，只删除不可见控制字符、网址、Emoji 与明确的 Markdown 项目符号。最终字幕和 TTS 会使用同一份对应 Provider 的净化文本，任务目录中可查看 `text_tts_ready.txt`。

“生词词典”默认关闭，且仅在 AI 洗稿开启时运行。开启后，程序只从洗稿并净化后的文本中确定性筛选连续 3 个以上汉字、或汉字与片假名混合的候选词，再请求文本模型返回假名读音。词典默认继承统一文本 API、统一 Key 和文本模型；开启“使用独立词典 API”后，可以单独填写 Base URL、模型和 Key，独立 Key 留空时仍继承统一 Key。制作词典提示词可以在 GUI 中编辑。程序绝不会采用候选列表以外的普通词；API 失败或输出不合规则时会直接保留 Edge 原文朗读，不会中断任务。生成结果写在任务内的 `tts_auto_pronunciation_dictionary.txt` 和 `tts_auto_pronunciation_report.json`，且手动导入词典对同名词有更高优先级。

### 批量导入正文和词典

点击“批量导入正文+词典”，可以在一个文件选择窗口中混合选择多篇正文和对应词典。最稳妥的命名规范是：

```text
作品名_正文.txt
作品名_读音词典.txt
```

也支持正文不带后缀：

```text
作品名.txt
作品名_读音词典.txt
```

程序会识别 `读音词典`、`詞典`、`TTS読み方辞書`、`dictionary` 等词典后缀，并在配对时忽略正文侧的 `正文`、`原文`、`本文`、`最终文本`、`日语`、`日本語` 等尾缀。例如下面这组现有命名也能自动配对：

```text
三国志演義第一话日语_最终文本.txt
三国志演義第一话_TTS読み方辞書.txt
```

配对采用“去掉这些后缀后的作品名完全一致”规则，不使用容易配错的模糊匹配。匹配到唯一词典的正文会自动附加词典；正文没有对应词典、词典格式无效或存在多个同名词典时，正文仍会按普通方式建立任务，只是不附加词典。没有对应正文的词典会忽略并在导入结果中列出。若选中的文件中完全没有词典，则维持普通多篇 TXT 导入方式。

## 续跑与排错

每次完整流水线会生成：

```text
data/jobs/{输入文件名}/
  status.json
  log.txt
  novel.json
  segments.json
  imported_audio.json               # 可选，导入 MP3 的任务才有
  imported_audio_timing.json        # 可选，导入 MP3 的估算时间轴
  audio/imported_narration.mp3      # 可选，导入后复制的完整音频
  tts_pronunciation_dictionary.txt  # 可选，仅供任务内部 TTS 使用
  durations.json
  prompts.json
  subtitle.ass
  subtitle.srt
  {输入文件名}.mp4
```

从 GUI 导入 TXT 时，工程目录和最终视频会沿用输入文件名（不含 `.txt`）。如果已经存在同名工程，
新工程目录会自动添加 `_2`、`_3`，不会覆盖旧工程；目录内的视频仍保留原输入文件名。

如果任务中断，在“完整流水线”里填入同一个 `job_id` 到“续跑 job_id”，再次启动即可。已有音频和图片会自动复用。

## 主要目录

```text
app/
  scrapers/
    qingtian.py       国内聚合接口
    legado.py         普通 Legado 静态 JSON 书源
    syosetu.py        日本 Syosetu
    kakuyomu.py       日本 Kakuyomu
  backends/
    tts.py
    llm.py
    image.py
  stages/
    stage2_clean.py
    stage_pacing.py
    stage6_compose.py
  pipeline_runner.py
data/jobs/
```

## 稳定性增强

当前版本已吸收 HUANCUN MVP 中最适合本项目的轻量机制：

- 完整流水线从桌面 GUI 派发为独立 worker 进程，GUI 关闭或重启时，已派发的长任务不会因为界面进程结束而立刻丢失。
- `data/runtime/locks/` 提供跨进程资源锁，默认普通电脑只允许 1 个 FFmpeg 任务并发，外部 API 默认 2 路，避免多任务抢满 CPU、内存和接口额度。
- 所有重试方式都会保留上一版成功的 MP4。新视频先合成到临时文件，完成时长校验后才原子覆盖旧视频；重试失败、被停止或中途反悔时，上一版成片仍可使用。“清除图片缓存”是明确的破坏性清理操作，仍会按确认提示删除成片。
- 本地 TXT 任务会在任务目录保存一份原文安全副本。原文件夹移动后，程序只会按唯一同名文件安全重连，找不到时改用校验通过的安全副本；本地路径绝不会降级交给网络书源。抓取结果、续跑缓存和 TTS 启动前还会核对来源类型，并拦截重复登录限制、会员广告或书源推广正文，防止错书继续消耗文字、图片 API 和配音时间。
- `data/jobs/{job_id}/audio/tts_manifest.json` 记录每段 TTS 的文本 hash、配置 hash、文件指纹和状态，续跑时优先复用已完成音频。
- TTS 卡在单段时，GUI 可先停止选中任务，再用“重试TTS段...”重置指定 `seg` 或失败/卡住/波形异常段；也可用“TTS卡段重试推进”重置未完成段并自动重新启动任务。
- `data/jobs/{job_id}/compose_manifest.json` 记录音频拼接和最终合成输入 hash，合成失败不会覆盖已有的最终视频。

普通配置电脑建议保持：

```text
Max FFmpeg slots = 1
Max external API slots = 1-2
Long video stable mode = enabled
Ken Burns = disabled for very long videos
```

FFmpeg 会按顺序查找：`MEDIA_FFMPEG_BIN` / `MEDIA_FFPROBE_BIN` 环境变量、系统 PATH、macOS 常见 Homebrew/本地路径、项目内 `runtime/ffmpeg`、`tools/ffmpeg`、`tools/ffmpeg/bin`、`vendor/ffmpeg`，最后才兼容旧的 `F:\Manao\drama_pipeline_release\tools\ffmpeg`。Windows 发包时把 `ffmpeg.exe` 和 `ffprobe.exe` 放进上述任一项目内目录即可；macOS 建议安装：

```bash
brew install ffmpeg-full
```

如果只有 `ffmpeg` 没有 `ffprobe`，本版本会用 `ffmpeg -i` 回退读取音频时长，但完整安装仍更稳。

## YouTube 自动上传

生成后自动上传可在桌面 GUI 的“流水线配置 -> 批量与上传”里开启。当前浏览器上传优先使用随包分发的：

```text
app/vendor/stage5_upload_browser.py
```

保留 `simple/full` 两种流程；`full` 流程会按配置尝试上传政策、广告位间隔、广告起始时间和公开范围。使用前需要 Chrome 调试端口 `9222` 可用，并已登录 YouTube Studio。

macOS 启动调试 Chrome：

```bash
scripts/start_chrome_debug_macos.command
```

如果本机没有安装 Google Chrome，`scripts/setup_macos.sh` 会下载 Playwright Chromium，调试启动脚本会自动使用它。

上传选择视频和封面时，macOS 会直接通过 Playwright 设置本地文件路径，不依赖原生文件选择框焦点，因此从桌面 GUI 发起上传更稳定。

## 封面生成

完整流水线会在 `data/jobs/{job_id}/cover/` 里生成：

```text
cover_prompt.txt
cover_prompt_attempts.json
cover_provider_raw.png
cover_raw.png
cover.jpg
cover_manifest.json
```

标题阶段会先读取开头、中段和结尾，一次生成 3 个标题、2 个概梗和内容匹配的标签，并在任务目录保存 `タイトル・あらすじ候補.txt` 与 `marketing_candidates.json`。标签不含自动前缀 `【朗読・小説】`。封面策划会比较全部候选，但只生成一条完整提示词；网络错误复用原提示词，只有明确的内容审核拒绝才重写安全版本，过程记录在 `cover_prompt_attempts.json`。上传时随机选中一个候选标题，先完整执行可编辑的上传标题模板，只有仍有字数空间时才补入模板中尚未出现的生成标签。

封面文字、字号层级、描边和题材配色作为提示词的一部分交给图片模型直接生成，不由程序后期压字。完整方法显示在 GUI 的“封面完整方法（可编辑）”中，其中可维护三国等题材的专用字体、颜色、底板与排版规则；程序不会再叠加隐藏的固定方法。封面提示词只要求生成真正的横向16:9画面，不固定像素尺寸，并要求文字、脸、头发、手和关键动作远离四边；接口原始返回会保存在 `cover_provider_raw.png`，最终文件尺寸按GUI的封面宽高配置处理。YouTube 自动上传时会从 3 个候选标题中为每个账号随机选择一个并持久化，重试不会换标题；模板文字和手写标签优先，生成标签只用于填充剩余标题空间。

可直接复制到 GUI 里使用的小说推文封面/视频图提示词示例见 `docs/tweet_prompt_templates.md`。

## API 检测

桌面 GUI 的配置页提供 TTS、分镜 LLM、图片、封面四组检测按钮。检测会优先访问 `/models` 或对应平台的模型/声音列表；分镜 LLM 在填写模型名后会做一次极小 `chat/completions` 测试。图片和封面检测默认不实际生成图片，避免误消耗出图额度。识别到候选模型且当前模型框为空时，GUI 会自动填入一个候选值。

检测到模型列表后，GUI 会把模型输入框切换成可选下拉列表，同时仍允许手动输入自定义模型名，方便使用中转站或私有模型别名。

图片 Provider 选择 `openai` 或 `custom` 时，会按 OpenAI 兼容格式调用：

```text
POST {base_url}/images/generations
```

如果一个任务已经生成过图片，流水线会复用 `data/jobs/{job_id}/images`，不会再次调用生图 API。更换中转站、模型或提示词后，先在任务队列点击“清除图片缓存”，再重新启动任务。

## 任务清理

桌面 GUI 的任务队列提供：

- 任务操作按钮区和任务表格均支持横向、纵向滚动，窄窗口也能查看全部按钮和列
- 删除选中任务
- 清空已结束任务
- 清除图片缓存

“清除图片缓存”会删除选中任务的图片、封面和最终视频，保留抓取文本与 TTS 结果，适合重新调用生图 API 或重新合成。
“重试TTS段...”只删除选中 TTS 段和受音频时长影响的下游缓存，保留图片；“TTS卡段重试推进”会重置未完成/失败/波形异常段并重新启动任务，不会生成静音占位。

## 标题与说明模板

流水线会读取小说开头、中段和结尾，生成 3 个标题、2 个概梗和标签，并写入 `metadata.json`、`marketing_candidates.json` 与可直接阅读的候选 TXT。新任务上传时直接使用持久化的随机候选标题；旧任务仍支持以下上传模板变量：

```text
{title}
{short_title}
{intro}
{author}
{tags}
{job_id}
```

例如标题模板可以写成 `{title}｜{short_title} #小说推文`，说明模板可以写成 `今日推荐：{short_title}\n\n{intro}\n\n{tags}`。

## 画面动作与字幕

图片动作支持上下、左右、单向推进、轻微缩放、静态和随机。启用移动时，每张图的运动会按该图实际持续时间走完整条曲线；例如 600 秒一张图，就是 600 秒内缓慢移动完成。默认曲线为 `ease`，会慢起慢停；也可以切到 `linear`。

最终视频固定内嵌 ASS 字幕，同时可选导出 SRT。字幕在最终 FFmpeg 合成阶段一次性烧录进视频，不是边生成边压字幕；这种方式适合长视频，内存占用相对稳定。GUI 中可配置字幕位置、字体、字号、颜色、描边、阴影、边距、每行字数和行数，并提供预览按钮。
