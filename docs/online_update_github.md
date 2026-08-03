# GitHub 线上更新发布说明

软件已经内置“软件更新”页。推荐使用公开 release 仓库发布更新包，GUI 从 `latest.json` 检查新版本。

macOS 更新到 1.0.21 及以上后，程序启动时会检测 FFmpeg 的 ASS 字幕能力。仅在缺少 `libass` 时通过 Homebrew 一次性安装并优先使用 `ffmpeg-full`；后续启动检测通过即跳过，不会重复安装。

## 仓库建议

推荐保持三仓库结构：

| 仓库 | 用途 |
| --- | --- |
| `1951779219/novel_video_pipeline` | 私有源码仓库 |
| `1951779219/novel_video_pipeline_mac_release` | 公开发布仓库，放更新 zip 和 `latest.json` |
| `1951779219/novel_video_pipeline_feedback` | 公开反馈仓库 |

GUI 默认读取：

```text
https://github.com/1951779219/novel_video_pipeline_mac_release/releases/latest/download/latest.json
```

如果换仓库，改 GUI 的 `更新清单 URL`，或改 `data/settings.json` 里的 `update_manifest_url`。

## 一次发布流程

维护机先安装 GitHub CLI 并登录：

```bash
brew install gh
gh auth login
```

然后在项目根目录运行：

```bash
RELEASE_REPO=1951779219/novel_video_pipeline_mac_release ./scripts/publish_github_release.sh
```

脚本会：

1. 读取 `app/version.py` 的 `VERSION`。
2. 生成脱敏更新包到 `dist/`。
3. 生成 `dist/latest.json`。
4. 在 GitHub Release `v{VERSION}` 上传 zip 和 `latest.json`。

如果 release 已存在，会用 `--clobber` 覆盖同名资产。

## 手动打包

只打包不上传：

```bash
python3 scripts/build_release_package.py --repo 1951779219/novel_video_pipeline_mac_release
```

输出：

```text
dist/novel_video_pipeline_mac_<version>_<date>.zip
dist/latest.json
```

`latest.json` 结构：

```json
{
  "version": "1.0.19-mac-update",
  "url": "https://github.com/1951779219/novel_video_pipeline_mac_release/releases/download/v1.0.19-mac-update/novel_video_pipeline_mac_1.0.19-mac-update_20260709.zip",
  "sha256": "...",
  "notes": "发布说明",
  "mandatory": false
}
```

## 用户配置保护

线上更新应用时只覆盖程序文件：

- `app/`
- `docs/`
- `scripts/`
- `AGENTS.md`
- `README.md`
- `Open_GUI.command`
- `Install_Mac_Dependencies.command`
- `requirements.txt`
- `update.bat`

这些不会覆盖：

- `data/settings.json`
- `data/profiles/`
- `data/jobs/`
- `data/projects/`
- `data/runtime/`
- `data/chrome_debug_profile/`
- `data/chrome_debug_profiles/`
- `.venv/`

更新前会自动备份：

```text
data/backups/pre_update_<version>_<time>/settings.json
data/backups/pre_update_<version>_<time>/profiles/
```

更新后会写：

```text
data/updates/last_update.json
data/updates/apply_update_<version>_<time>.log
```

## 发布包脱敏规则

发布脚本会清空这些字段：

- `ai_api_key`
- `llm_api_key`
- `tts_api_key`
- `image_api_key`
- `cover_api_key`
- `character_reference_api_key`
- `scene_reference_api_key`

并排除：

- `data/jobs/`
- `data/projects/`
- `data/runtime/`
- `data/chrome_debug_profile/`
- `data/chrome_debug_profiles/`
- 本地浏览器登录数据

## 验证

发布前至少运行：

```bash
python3 -m py_compile app/config.py app/gui.py app/updater.py app/update_tab.py scripts/apply_update.py scripts/build_release_package.py
python3 -m compileall app
python3 scripts/build_release_package.py --repo 1951779219/novel_video_pipeline_mac_release
```

检查包内是否泄露密钥：

```bash
python3 - <<'PY'
import re, zipfile
from pathlib import Path
zip_path = sorted(Path("dist").glob("novel_video_pipeline_mac_*.zip"))[-1]
leaks = []
with zipfile.ZipFile(zip_path) as z:
    for name in z.namelist():
        if name.endswith((".py", ".json", ".md", ".command", ".sh", ".txt")):
            text = z.read(name).decode("utf-8", errors="ignore")
            if re.search(r"sk-[A-Za-z0-9]{20,}", text):
                leaks.append(name)
print(zip_path)
print("leaks:", leaks)
PY
```
