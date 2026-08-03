# 分包机器 Agent 升级与制作说明

这份文档给其它 Mac 或分包机器上的维护 agent 使用，用来快速理解项目、修改模块、验证和重新打包。

## 项目边界

这是桌面 GUI 优先的软件。默认入口是：

```bash
./Open_GUI.command
```

不要把用户重新引导到旧 WebUI。浏览器只用于 YouTube Studio 上传、登录和排错。

## 推荐检索关键词

```bash
rg -n "ai_rewrite_|cover_prompt|llm_storyboard|character_analysis|browser_profiles|browser_upload_all_profiles|stage_upload|upload_via_browser|source_catalog|Syosetu|Kakuyomu" app docs data/settings.json
```

常用定位：

- GUI 字段：`app/gui.py`
- 默认配置和迁移：`app/config.py`
- 生产流水线：`app/pipeline_runner.py`
- 图片/TTS/LLM 后端：`app/backends/`
- 浏览器上传：`app/upload.py`, `app/vendor/stage5_upload_browser.py`
- 软件更新：`app/updater.py`, `app/update_tab.py`, `scripts/apply_update.py`
- 文档：`docs/`

## 统一 AI 接口

schema 17 增加统一接口，减少重复填写：

- `ai_api_enabled`
- `ai_api_base_url`
- `ai_api_key`
- `ai_api_text_model`
- `ai_api_image_model`

运行时文本模型统一走 `app/pipeline_runner.py` 的 `_llm_route_settings()`；OpenAI 兼容图片统一走 `_image_route_settings()` 和 `_apply_unified_image_api()`。新增文本/图片调用时，优先复用这些 helper，不要再直接读取 `llm_api_key` 或 `image_api_key`。

## 修改配置字段的标准流程

1. 在 `app/config.py` 的 `DEFAULT_SETTINGS` 添加默认值。
2. 如需兼容旧配置，提升 `SETTINGS_SCHEMA_VERSION` 并在 `_apply_compat_migrations` 添加迁移。
3. 在 `app/gui.py` 添加表单字段，并在 `_config_key_sets` 中登记 int/float/bool 类型。
4. 在 `app/pipeline_runner.py` 或对应模块读取配置。
5. 同步更新 `data/settings.json` 和 `data/profiles/*.json` 的示例值，发布前清空 API key。
6. 更新相关 `docs/*.md`。

## 修改提示词的标准流程

提示词字段不要硬编码在生成函数里，优先放进 `app/config.py`，再在 GUI 暴露。需要缓存失效时，把新字段纳入对应阶段的配置签名，避免继续复用旧图。

重点字段见 `docs/module_prompt_editing_guide.md`。

## 浏览器上传升级注意

浏览器上传分两层：

- `app/upload.py` 是项目包装层，负责把主流程配置转成上传脚本能读的 config/profile。
- `app/vendor/stage5_upload_browser.py` 是具体 Playwright 自动化脚本。

macOS 兼容点不要丢：

- `_find_chrome_exe()` 要能找到 `/Applications/Google Chrome.app` 和 Playwright Chromium。
- 非 Windows 文件上传优先走 Playwright `filechooser` / `set_input_files`。
- 非 Windows 不要调用 `ctypes.windll`、`taskkill` 或 PowerShell。
- 多账号上传不要并发抢 `9222` 端口，按方案顺序执行。

多账号配置见 `docs/browser_multi_account_upload.md`。

## GitHub 线上更新

线上更新分两段：

- GUI 检查/下载/应用：`app/update_tab.py` 调用 `app/updater.py`。
- 发布新版本：`scripts/build_release_package.py` 生成脱敏 zip 和 `latest.json`，`scripts/publish_github_release.sh` 用 GitHub CLI 上传 release 资产。

macOS 应用更新不再依赖 Windows `update.bat`，而是由 `scripts/apply_update.py` 执行。它只覆盖程序文件，保护 `data/settings.json`、`data/profiles/`、`data/jobs/`、`data/projects/`、Chrome 登录态和 `.venv`。更新前会备份设置到 `data/backups/pre_update_*`。

完整发布说明见 `docs/online_update_github.md`。

## 验证命令

基础验证：

```bash
python3 -m py_compile app/config.py app/gui.py app/pipeline_runner.py app/upload.py app/vendor/stage5_upload_browser.py app/youtube_ad_suitability.py
python3 -m py_compile app/updater.py app/update_tab.py scripts/apply_update.py scripts/build_release_package.py
python3 -m compileall app
```

上传模块导入验证：

```bash
python3 - <<'PY'
from app import upload
mod = upload._load()
print(mod.__name__)
print("upload", hasattr(mod, "upload_via_browser"))
print("mac_file_selector", hasattr(mod, "_select_file_with_playwright"))
from app.youtube_ad_suitability import normalize_ad_suitability_template
print(normalize_ad_suitability_template({"default": "2", "questions": {"暴力": "1"}}))
PY
```

配置验证：

```bash
python3 - <<'PY'
from app.config import config
for key in ["settings_schema_version", "browser_chrome_profile", "browser_upload_all_profiles", "browser_profiles"]:
    print(key, config.get(key))
PY
```

## API Key 故障排查

如果日志出现：

```text
Illegal header value b'Bearer ...\n'
```

原因是 API Key 末尾带了换行或空格，请在 GUI 中重新粘贴并保存配置。schema 16 之后，GUI 保存、配置加载、LLM/图片/TTS 后端都会自动 `strip()` key，日志也会把 `Bearer ...` / `sk-...` 脱敏。

如果日志是：

```text
HTTP 401: Invalid token
```

说明请求已经发到接口，但服务端认为 token 无效。优先检查 Base URL 和 Key 是否属于同一个中转站/服务商，以及账号余额、模型权限是否可用。

## 打包原则

发布包应该包含：

- `AGENTS.md`
- `README.md`
- `Open_GUI.command`
- `Install_Mac_Dependencies.command`
- `app/`
- `docs/`
- `scripts/`
- `requirements.txt`
- 脱敏后的 `data/settings.json`
- 脱敏后的 `data/profiles/*.json`

发布包不应该包含：

- `data/jobs/`
- `data/projects/`
- `data/runtime/`
- `data/chrome_debug_profile/`
- `data/chrome_debug_profiles/`
- API key、token、浏览器登录态。

打包后至少检查：

```bash
unzip -l dist/<package>.zip | sed -n '1,120p'
```

再扫密钥模式，确认没有 `sk-...`、`AKIA...`、`AIza...` 之类内容。

## 交付说明建议

交付给操作员时只写最关键的信息：

- 包路径。
- 如何启动。
- 这次改了哪些模块。
- 运行过哪些验证。
- 哪些操作仍需要账号登录或人工确认。
