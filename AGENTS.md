# Agent Maintenance Notes

This project is a macOS-first desktop GUI pipeline for turning novel text into recap videos. Use `Open_GUI.command` as the normal operator entrypoint. The browser upload path is only for YouTube Studio publishing and should not be confused with the retired WebUI flow.

## Start Here

- Operator docs: `README.md`
- Module and prompt editing guide: `docs/module_prompt_editing_guide.md`
- Multi-account browser upload guide: `docs/browser_multi_account_upload.md`
- GitHub online update guide: `docs/online_update_github.md`
- Upgrade and packaging playbook: `docs/agent_upgrade_playbook.md`
- Prompt examples: `docs/tweet_prompt_templates.md`

## Important Runtime Files

- Main GUI: `app/gui.py`
- Pipeline stages: `app/pipeline_runner.py`
- Config defaults and migrations: `app/config.py`
- Browser upload wrapper: `app/upload.py`
- Browser upload implementation: `app/vendor/stage5_upload_browser.py`
- YouTube ad-suitability helper: `app/youtube_ad_suitability.py`
- Online updater: `app/updater.py`, `app/update_tab.py`, `scripts/apply_update.py`, `scripts/build_release_package.py`
- Unified AI API fields: `ai_api_enabled`, `ai_api_base_url`, `ai_api_key`, `ai_api_text_model`, `ai_api_image_model`
- User settings: `data/settings.json`
- User profiles: `data/profiles/*.json`

## Safe Validation Bundle

Run these after code changes:

```bash
python3 -m py_compile app/config.py app/gui.py app/pipeline_runner.py app/upload.py app/vendor/stage5_upload_browser.py app/youtube_ad_suitability.py app/updater.py app/update_tab.py scripts/apply_update.py scripts/build_release_package.py
python3 -m compileall app
python3 - <<'PY'
from app import upload
mod = upload._load()
print(mod.__name__, hasattr(mod, "upload_via_browser"), hasattr(mod, "_select_file_with_playwright"))
from app.config import config
print(config.get("settings_schema_version"), config.get("browser_chrome_profile"))
from app.utils.secrets import redact_secret_text
print(redact_secret_text("Bearer sk-example1234567890"))
PY
```

When packaging for another Mac, scrub API keys in `data/settings.json` and `data/profiles/*.json`. Do not package `data/jobs`, `data/projects`, `data/chrome_debug_profile`, runtime caches, or local browser login data.
