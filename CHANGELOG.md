# Changelog

## v1.0.22-mac-update

- Added TXT + prepared MP3 import in the desktop GUI, allowing an existing TTS narration and its source text to bypass speech generation and continue through image planning, image generation, subtitles, cover creation, and final video composition.
- Copies imported narration into the job and estimates subtitle/image timing against the complete MP3 duration so the task can be resumed safely after the original files move.
- Hardened release packaging so it generates clean default settings and a generic profile without reading or publishing local user configuration.

## v1.0.18

- Added automatic story and character analysis that writes per-job `character_profiles.json` with protagonists, supporting roles, fixed visual profiles, triggers, and task-level visual theme.
- Added optional character reference image generation and `character_reference_manifest.json`, with desktop GUI controls for provider, model, dimensions, and prompt suffix.
- Unified image routing for scene images, covers, character references, and scene-reference generation across ComfyUI, SDWebUI, OpenAI-compatible, custom, and placeholder providers.
- Added OpenAI-compatible `/images/edits` and SDWebUI img2img reference-image paths so generated character references can be reused by scene generation when enabled.
- Documented the Wenjing Huashi static-analysis takeaways and the character visual pipeline roadmap, and added a full-package helper script.

## v1.0.17

- Align image API timeout defaults to 300 seconds across GUI, runner, and backend fallback paths so legacy or unset configs do not keep image requests waiting for the older 900-second default.

## v1.0.16

- Bound TTS external API slot waits by `tts_segment_timeout_seconds` so stuck slot contention no longer leaves narration segments waiting indefinitely.
- Mark TTS manifest entries as `waiting_slot` until they actually acquire an external API slot, so long waits are easier to distinguish from an active speech request.
- Bound image external API slot waits by `image_api_timeout_seconds` so image generation can fall back to the existing placeholder path instead of hanging forever behind a stuck slot.

## v1.0.15

- Treat Playwright/CDP disconnects during YouTube upload retry navigation as a Chrome crash so the outer upload loop restarts Chrome instead of reusing a dead page.

## v1.0.14

- Runs Edge TTS synthesis in a child process so `tts_segment_timeout_seconds` can hard-stop stuck segments and fall back through the existing retry/silence path.
- Cleans leading title/author/intro scaffold text and standalone chapter headings before segmentation.
- Uses an already generated video image as the cover fallback when cover image generation fails, before falling back to a placeholder.
- Reduces the default image API timeout from 900 seconds to 300 seconds for legacy configs that still use the old default.

## v1.0.13

- Changed the default concurrency model to single-job-first so one job gets the TTS, image, and video parallelism budget instead of splitting resources across multiple jobs by default.
- Exposed simultaneous job count and per-job TTS/image/video parallel controls in both desktop GUI and WebUI.
- Added per-job performance timing output (`performance.json` plus a log summary) to identify whether TTS, image generation, cover, compose, or upload is the bottleneck.

## v1.0.12

- Cleaned generated subtitle text so only commas and periods remain; other punctuation is converted to spacing before ASS/SRT output.
- Removed the full-width dark cover title band and updated cover prompts to request an uncluttered title area instead of a dark lower area.

## v1.0.11

- Changed default image and video dimensions to 16:9 landscape (`1920x1080`) and migrates the old `1080x1920` defaults.
- Added policy-safe image prompt rewriting and safer retry prompts to reduce content-policy failures from violent, sexual, or graphic story text.
- Updated image progress logging to emit per completed image plus heartbeat logs while image requests are still running.
- Removed trailing author/source parentheses from cover and YouTube upload titles by using a clean public title.
- Auto-starts the next queued task when a worker completes successfully and capacity is available.

## v1.0.10

- Added a configurable per-segment TTS timeout so a stuck Edge/API speech request can fail, retry, and use the existing silence fallback instead of holding the whole job indefinitely.
- Added TTS heartbeat logs while long-running speech tasks are in progress, including which segments are still active.
- Updated TTS progress logging to emit after every completed segment so the desktop GUI and WebUI keep showing movement during long narration jobs.
- Exposed the TTS timeout and heartbeat settings in the desktop GUI and WebUI.

## v1.0.9

- Added `max_concurrent_jobs` configuration (default 2) to control how many worker processes may run simultaneously, preventing resource exhaustion while allowing parallel processing.
- Added a "retrying" log message when a TTS segment fails and will be retried, so users can see that the system is still working rather than stuck.
- Added a placeholder hint in the desktop GUI log tab when no job is selected, making it clearer how to view real-time task logs.

## v1.0.8

- Changed the default text import limit to unlimited and migrates the legacy 8000-character default so full local TXT files are processed by default.
- Updated default YouTube title and description templates to the requested short-video format, with upload titles capped around 100 characters.
- Added a cross-process upload queue lock so multiple completed jobs upload to YouTube one at a time.
- Runs the full dependency check before browser upload so missing Playwright/Chrome prerequisites can be repaired before the upload step.

## v1.0.7

- Preserve generated image aspect ratio when fitting OpenAI-compatible image results into configured video or cover dimensions, preventing 16:9 covers from being stretched.
- Improved cover title rendering with a softer gradient title band, accent line, shadow, and stroke for better readability and visual blending.
- Added a warning when imported text reaches `scraper_max_chars`, making partial-video reports easier to diagnose without changing generation length or API usage.

## v1.0.6

- Improved OpenAI-compatible image generation errors so HTTP failures include a redacted response summary and a model hint when an unsupported `gpt-image-*` model may be configured.

## v1.0.5

- Added a user-facing production-stage display in the desktop GUI and WebUI so users can see whether a task is reading text, cleaning, generating speech, generating images, composing video, or uploading.
- Included the readable production stage in task status data to make stuck or long-running jobs easier to diagnose.

## v1.0.4

- Fixed duplicate job IDs when importing multiple TXT files in the same second, so queued batch imports no longer overwrite each other.

## v1.0.3

- Added task stop controls for running workers in the desktop GUI and WebUI.
- Running tasks can now be stopped before deletion, including process-tree termination on Windows.
- Deleting a running task now stops the worker first and retries directory removal.

## v1.0.2

- Added cache-busting to update manifest checks so GitHub `latest.json` does not get stuck on an older release.

## v1.0.1

- Added the public feedback entry for users to submit bugs and feature requests.
- Added GitHub Issue templates plus automatic triage labels for feedback issues.
- Added the feedback/update closed loop documentation.

## v1.0.0

- Added GitHub release packaging for online update bundles.
- Added an in-app updater that reads `latest.json`, verifies SHA256, downloads the update zip, and launches `update.bat`.
- Kept local runtime data, generated jobs, caches, and FFmpeg binaries out of the GitHub source repository.
