# 模块与提示词修改教程

这份文档给操作员和后续维护 agent 使用。目标是回答两个问题：哪些功能模块在哪里改，哪些提示词可以在 GUI 或配置文件里改。

## 入口和保存位置

正常使用入口是项目根目录的 `Open_GUI.command`。GUI 中点击“保存配置”会写入 `data/settings.json`，点击“保存到当前方案”会同步写入 `data/profiles/配置1.json` 这类方案文件。

配置优先级大致是：

1. 当前 GUI 表单值。
2. `data/settings.json`。
3. 当前方案 `data/profiles/*.json`。
4. `app/config.py` 里的默认值和迁移逻辑。

如果要给其它 Mac 分发默认配置，改 `app/config.py` 和发布包里的脱敏 `data/settings.json`；如果只改本机当前使用习惯，改 GUI 后保存即可。

## 模块位置速查

| 功能 | GUI 区域 | 主要文件 | 常见输出 |
| --- | --- | --- | --- |
| 小说搜索/采集 | 小说来源、搜索框 | `app/scrapers/`, `app/scrapers/source_catalog.py` | 导入文本、任务目录 |
| 洗稿/改写 | AI 洗稿 | `app/pipeline_runner.py`, `app/backends/llm.py` | `text_rewritten.txt`, `text_rewrite_report.json` |
| 分段/节奏 | 视频节奏、批量与上传 | `app/pipeline_runner.py`, `app/stages/stage_pacing.py` | `segments.json`, `plans.json` |
| TTS | TTS 配置 | `app/backends/tts.py`, `app/tts_worker.py`, `app/pipeline_runner.py` | `audio/*.mp3`, `tts_manifest.json` |
| 人设分析 | 图片与提示词 | `app/character_analysis.py`, `app/pipeline_runner.py` | `character_profiles.json` |
| 人设参考图 | 图片与提示词 | `app/backends/image.py`, `app/pipeline_runner.py` | `character_reference_manifest.json` |
| 剧情图 | 图片与提示词 | `app/backends/image.py`, `app/pipeline_runner.py` | `images/`, `prompts.json` |
| 封面 | 封面配置 | `app/backends/image.py`, `app/pipeline_runner.py` | `cover/` |
| 合成视频 | 字幕/视频配置 | `app/stages/stage6_compose.py`, `app/pipeline_runner.py` | `{输入文件名}.mp4`, `subtitle.srt` |
| 浏览器上传 | 批量与上传 | `app/upload.py`, `app/vendor/stage5_upload_browser.py` | `upload_result.json` |
| 依赖检测 | 依赖检测 | `app/dependency_manager.py`, `scripts/setup_macos.sh` | `data/dependency_report.json` |

## GUI 里可修改的主要提示词

这些字段都可以在 GUI 保存，也可以直接在 `data/settings.json` / `data/profiles/*.json` 中修改。

## 统一 AI 接口

GUI 的“API 设置与 Key”区域顶部有一组统一接口字段，并在同一区域集中管理 TTS、分镜、图片、人设图、剧情参考图和封面的 Provider、Base URL、API Key 与模型：

- `ai_api_enabled`：开启后，文本生成和 OpenAI 兼容生图默认共用统一接口。
- `ai_api_base_url`：统一 Base URL/中转地址，例如 `https://你的中转站/v1`。
- `ai_api_key`：统一 API Key，只需要填一次。
- `ai_api_text_model`：洗稿、短标题、人设分析、分镜提示词、封面分析使用的文本模型。
- `ai_api_image_model`：剧情图、封面、人设图、剧情参考图使用的图片模型。

GUI 中所有 API Key 默认用黑点遮挡；可通过输入框右侧的小眼睛临时显示/隐藏，或点击“复制”直接复制完整 Key。

模块自己的 `llm_base_url` / `llm_api_key` / `image_base_url` / `image_api_key` 等字段仍保留，用于高级覆盖或本地服务。普通分包机建议只填统一接口，图片 Provider 选择 `openai` 或 `custom`，封面/人设/剧情参考保持 `same_as_image` 即可。

各业务分组仍保留对应的模型输入框。如果它与“API 设置与 Key”里的同名模型填写不同，保存配置时 GUI 会要求选择最终使用的模型，然后同步两处。

## OpenAI / gpt-image-2 是否需要 workflow

不需要。`gpt-image-1`、`gpt-image-2`、DALL-E 或其它 OpenAI 兼容图片接口，运行时会直接把最终提示词发给图片接口生成：

- 人设图使用人设分析得到的角色提示词 + `character_reference_prompt_suffix`。
- 剧情图使用通用前缀、段落分析提示词、人设锁和视觉主题。
- 剧情参考图如果有参考图，会走 OpenAI 兼容图片编辑接口；没有参考图则走普通提示词生图。
- 封面会自动取文章标题和正文摘要，经过 `cover_prompt_template` / `cover_ai_analysis_prompt` 后，把“标题作为图片内容的一部分”交给图片模型生成。

GUI 里的 `人设图 workflow(仅 ComfyUI)`、`剧情参考 workflow(仅 ComfyUI)` 和全局 `image_workflow` 只给 ComfyUI 使用。Provider 选择 `openai` / `custom` / `same_as_image` 时，不需要填写 workflow；只要统一接口或模块自己的 Base URL、API Key、模型名正确即可。

| 字段 | 用途 | 可用变量 |
| --- | --- | --- |
| `ai_rewrite_prompt` | 洗稿/改写文章，替代旧的“扫文清理”逻辑 | `{text}` 由代码分批传入 |
| `short_title_prompt` | 自动生成短说明标题 | 输入文章标题和正文摘要 |
| `character_analysis_prompt` | 分析主角、配角、人设、视觉主题 | 原文或截断后的文章内容 |
| `character_reference_prompt_suffix` | 生成人设参考图时追加的风格限制 | 人设 JSON 中的角色字段 |
| `llm_image_prompt_prefix` | 每张剧情图的通用前缀 | 会拼到剧情图提示词前 |
| `llm_storyboard_prompt` | 剧情图 AI 分析的系统提示词 | 控制分镜图分析规则 |
| `llm_storyboard_user_template` | 剧情图 AI 分析的用户模板 | `{prefix}`, `{style}`, `{index}`, `{text}` |
| `cover_prompt_template` | 封面生成的主模板 | `{title}`, `{excerpt}`, `{style}`, `{custom}` |
| `cover_custom_prompt` | 封面模板里的自定义方向 | 由 `{custom}` 引用 |
| `cover_ai_analysis_prompt` | 封面 AI 分析模板 | 标题和正文摘要 |
| `youtube_title_template` | YouTube 上传标题模板 | `{clean_title}`, `{title}`, `{short_title}`, `{intro}` |
| `youtube_description` | YouTube 上传说明模板 | 同上传标题上下文 |
| `browser_ad_suitability_template` | YouTube 广告分级问卷选择模板 | JSON，见上传文档 |

## 封面提示词怎么改

封面现在不是把标题作为后期文本盖到图片上，而是把标题和正文摘要一起交给图片模型，让模型在画面里生成标题和主体视觉。推荐改法：

1. 在 GUI 的封面区域修改 `cover_prompt_template`。
2. 保留 `{title}`，这样任务标题会进入封面生成提示词。
3. 用 `{excerpt}` 让模型理解剧情气质。
4. 用 `{custom}` 承接可变风格，比如“悬疑反转”“古风权谋”。
5. 保存配置后，如果旧封面缓存还在，使用 GUI 的“清理媒体缓存”或删除任务目录里的 `cover/` 后重跑封面阶段。

示例可看 `docs/tweet_prompt_templates.md`。

## 剧情图提示词怎么改

### 视频生图解决方案1（高光点选图）

开启“智能选择旁白高光点（不增加图片数）”后，`pacing_seconds_per_image` 只决定图片预算。例如总旁白 1800 秒、每图 300 秒，仍生成约 6 张图。程序先生成 `story_visual_context.json`，再从每个预算窗口的带编号旁白段中选择 1～3 个连续段作为唯一高光事件。全篇背景只能约束时代、世界观和关系，不能向当前画面添加旁白高光中没有的人物或事件。

开启“按高光旁白调整换图时间”后，程序会在图片总数不变、视频总时长不变的前提下，把相邻图片的切换点调整到两个高光时间的中间。最终选择结果记录在 `prompts.json` 和 `plans.json` 的 `highlight_*` 字段中。

旧任务已有图片时仍会复用缓存。要让旧任务应用本方案，需要先在任务队列点击“清除图片缓存”再重新运行。

剧情图由两层组成：

1. `llm_image_prompt_prefix`：每张图都会带上的通用前缀，比如画风、角色年龄、无文字、构图要求。
2. `llm_storyboard_user_template`：把当前小说段落变成单张分镜图提示词的任务模板。

如果打开了人设分析，`character_profiles.json` 中的 `visual_theme` 和 `characters[].visual_prompt_en` 会继续注入剧情图，避免主角每张图长得不一样。

修改建议：

- 只想改整体画风，优先改 `llm_image_prompt_prefix`。
- 想改 AI 如何理解段落，改 `llm_storyboard_prompt` 和 `llm_storyboard_user_template`。
- 想固定主角外貌，改 `character_analysis_prompt` 或重新生成 `character_profiles.json`。
- 改完提示词后清理旧图片缓存，否则可能复用旧图。

## 洗稿提示词怎么改

GUI 里显示为 AI 洗稿。对应配置：

- `ai_rewrite_enabled`：是否启用。
- `ai_rewrite_batch_chars`：每批处理字符数。
- `ai_rewrite_prompt`：洗稿任务说明。

洗稿发生在切片和 TTS 前。输出会写到任务目录的 `text_rewritten.txt`，报告写到 `text_rewrite_report.json`。如果没有 LLM key，或当前后端不可用，会跳过而不是中断整条流水线。

## 修改后的验证方法

修改配置或提示词后推荐检查：

```bash
python3 -m py_compile app/config.py app/gui.py app/pipeline_runner.py
python3 -m compileall app
```

然后用 GUI 新建一个短文本任务，至少确认：

- `prompts.json` 里出现新提示词或新前缀。
- `character_profiles.json` 里人设输出合理。
- `cover/` 里的封面是新风格。
- 与输入文件同名的 `.mp4` 能生成。

## 常见问题

如果改了提示词但图没变，多半是缓存命中。清理任务媒体缓存后重跑。

如果提示词里变量没有替换，检查花括号拼写是否和上面的变量表一致。

如果 AI 输出过短，检查对应的 token 上限，例如 `character_analysis_max_tokens`，以及后端模型是否限制输出长度。
