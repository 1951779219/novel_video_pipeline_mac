import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from app import config as config_module
from app import pipeline_runner
from app import project_manager
from app.gui import PipelineGUI
from app.character_analysis import normalize_analysis
from app.backends.image import ImageBackend
from app.stages.stage6_compose import _video_encoder_args
from app.storyboard_highlights import parse_json_object


class ImagePromptRegressionTests(unittest.TestCase):
    def test_repeated_safety_pass_preserves_three_kingdoms_scene(self):
        scene = (
            "Depict mainland Chinese late Han dynasty people and places only; never Japanese samurai or kimono. "
            "Scene-specific Three Kingdoms event: Lu Bu catches Zhang Liao's blade between two fingers."
        )
        prompt = pipeline_runner._policy_safe_image_prompt(scene)
        prompt = pipeline_runner._policy_safe_image_prompt(prompt)
        prompt = pipeline_runner._policy_safe_image_prompt(prompt)

        self.assertIn("Lu Bu catches Zhang Liao's blade", prompt)
        self.assertIn("mainland Chinese late Han dynasty", prompt)
        self.assertEqual(prompt.count("Strict final requirement:"), 1)
        self.assertLessEqual(len(prompt), 1400)

    def test_long_repeated_prompt_keeps_scene_tail(self):
        prefix = "historical illustration, " * 80
        scene = "SCENE_ANCHOR Cao Cao raises a jade bracelet inside a late Han residence."
        prompt = pipeline_runner._policy_safe_image_prompt(prefix + scene)
        prompt = pipeline_runner._policy_safe_image_prompt(prompt)
        self.assertIn("SCENE_ANCHOR", prompt)


class EncoderQualityRegressionTests(unittest.TestCase):
    def test_videotoolbox_inverts_crf_style_quality_scale(self):
        args_20 = _video_encoder_args("h264_videotoolbox", quality=20)
        args_30 = _video_encoder_args("h264_videotoolbox", quality=30)
        q20 = int(args_20[args_20.index("-q:v") + 1])
        q30 = int(args_30[args_30.index("-q:v") + 1])
        self.assertEqual(q20, 62)
        self.assertGreater(q20, q30)

    def test_x264_keeps_crf_value(self):
        args = _video_encoder_args("libx264", quality=20)
        self.assertEqual(args[args.index("-crf") + 1], "20")


class JsonCompatibilityRegressionTests(unittest.TestCase):
    def test_extracts_first_object_with_trailing_commentary(self):
        self.assertEqual(parse_json_object('Result: {"action": "ride"}\nDone.'), {"action": "ride"})

    def test_accepts_double_encoded_object(self):
        self.assertEqual(parse_json_object('"{\\"action\\": \\"ride\\"}"'), {"action": "ride"})

    def test_empty_upload_description_survives_config_migration(self):
        data = dict(config_module.DEFAULT_SETTINGS)
        data["youtube_description"] = ""
        config_module._apply_compat_migrations(data, {"settings_schema_version": 1})
        self.assertEqual(data["youtube_description"], "")


class NovelProjectRegressionTests(unittest.TestCase):
    def test_new_project_locks_manual_shared_novel_title_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(project_manager, "PROJECTS_DIR", root / "projects"):
                project = project_manager.create_project(
                    "内部管理名",
                    series_video_settings={
                        "shared_novel_title": "人工统一小说名",
                    },
                )
                loaded = project_manager.load_project(project["project_id"])

        settings = loaded["series_video_settings"]
        self.assertEqual(settings["shared_novel_title"], "人工统一小说名")
        self.assertTrue(settings["shared_novel_title_locked"])

    def test_locked_shared_title_skips_series_name_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs"
            jobs.mkdir()
            with patch.object(project_manager, "PROJECTS_DIR", root / "projects"), patch.object(
                pipeline_runner, "JOBS_DIR", jobs
            ):
                project = project_manager.create_project(
                    "内部管理名",
                    series_video_settings={
                        "shared_novel_title": "人工统一小说名",
                        "shared_novel_title_locked": True,
                    },
                )
                job_dir = jobs / "episode_1"
                pipeline_runner.write_status(
                    job_dir,
                    project_id=project["project_id"],
                    series_title="人工统一小说名",
                    series_episode=1,
                )
                with patch.object(
                    pipeline_runner,
                    "_can_call_text_llm",
                    side_effect=AssertionError("不应检查或调用系列名 AI"),
                ):
                    result = pipeline_runner._series_short_name_from_existing_titles(
                        job_dir,
                        ["每集标题"],
                        lambda _message: None,
                    )

        self.assertEqual(result, "人工统一小说名")

    def test_series_title_and_cover_templates_use_manual_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs"
            jobs.mkdir()
            with patch.object(project_manager, "PROJECTS_DIR", root / "projects"), patch.object(
                pipeline_runner, "JOBS_DIR", jobs
            ):
                project = project_manager.create_project(
                    "内部管理名",
                    series_video_settings={
                        "shared_novel_title": "人工统一小说名",
                        "shared_novel_title_locked": True,
                        "episode_label_style": "第{episode}集",
                        "upload_title_template": "{series_title}｜{episode_label}｜{ai_title}",
                        "cover_label_template": "{series_title}【{episode_label}】",
                    },
                )
                job_dir = jobs / "episode_2"
                pipeline_runner.write_status(
                    job_dir,
                    project_id=project["project_id"],
                    series_title="人工统一小说名",
                    series_episode=2,
                )
                result = pipeline_runner.apply_series_presentation(
                    {
                        "titles": ["这一集发生了重大逆转"],
                        "short_title": "这一集发生了重大逆转",
                    },
                    job_dir,
                )

        self.assertEqual(result["series_upload_prefix"], "人工统一小说名｜第2集｜")
        self.assertEqual(result["series_cover_label"], "人工统一小说名【第2集】")
        self.assertEqual(
            result["series_display_title"],
            "人工统一小说名｜第2集｜这一集发生了重大逆转",
        )

    def test_forced_project_import_assigns_sequence_from_configured_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "甲.txt"
            second = root / "乙.txt"
            first.write_text("正文", encoding="utf-8")
            second.write_text("正文", encoding="utf-8")
            with patch.object(project_manager, "PROJECTS_DIR", root / "projects"):
                project = project_manager.create_project(
                    "预建项目",
                    series_video_settings={
                        "shared_novel_title": "统一小说名",
                        "episode_start": 5,
                    },
                )
                gui = PipelineGUI.__new__(PipelineGUI)
                assignments = gui._project_assignments_for_forced_import(
                    project["project_id"],
                    [second, first],
                )

        self.assertEqual(
            {
                assignments[str(first.resolve(strict=False))]["episode"],
                assignments[str(second.resolve(strict=False))]["episode"],
            },
            {5, 6},
        )

    def test_disabling_ai_episode_titles_uses_local_fallback_without_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs"
            jobs.mkdir()
            with patch.object(project_manager, "PROJECTS_DIR", root / "projects"), patch.object(
                pipeline_runner, "JOBS_DIR", jobs
            ):
                project = project_manager.create_project(
                    "预建项目",
                    series_video_settings={
                        "shared_novel_title": "人工统一小说名",
                        "shared_novel_title_locked": True,
                        "ai_episode_title_enabled": False,
                    },
                )
                job_dir = jobs / "episode_1"
                pipeline_runner.write_status(
                    job_dir,
                    project_id=project["project_id"],
                    series_title="人工统一小说名",
                    series_episode=1,
                )
                novel = pipeline_runner.Novel(
                    site="local",
                    novel_id="episode-1",
                    title="原始标题",
                    author="",
                    description="",
                    chapters=[
                        pipeline_runner.NovelChapter(
                            index=1,
                            title="正文",
                            text=(
                                "主人公在会议上公开证据，局势发生逆转。所有人终于发现真正的幕后人物。"
                                "她随后说明证据的来源，并揭开多年前被隐藏的约定。"
                                "昔日的同伴重新站到她身边，反对者也不得不承认事实。"
                            ),
                        )
                    ],
                )
                segments = [
                    pipeline_runner.Segment(
                        index=0,
                        text=(
                            "主人公在会议上公开证据，局势发生逆转。所有人终于发现真正的幕后人物。"
                            "她随后说明证据的来源，并揭开多年前被隐藏的约定。"
                            "昔日的同伴重新站到她身边，反对者也不得不承认事实。"
                        ),
                    )
                ]
                with patch.object(
                    pipeline_runner,
                    "_can_call_text_llm",
                    side_effect=AssertionError("关闭后不应检查或调用标题 LLM"),
                ):
                    metadata = pipeline_runner.stage_metadata(
                        novel,
                        job_dir,
                        segments=segments,
                    )
                generation_attempts = json.loads(
                    (job_dir / "marketing_candidates.json").read_text(encoding="utf-8")
                )["generation_attempts"]

        self.assertTrue(metadata["titles"])
        self.assertEqual(generation_attempts, 0)

    def test_character_analysis_keeps_relationship_records(self):
        normalized = normalize_analysis({
            "characters": [{"name": "林月"}, {"name": "周宁"}],
            "relationships": [{
                "from": "林月",
                "to": "周宁",
                "relation": "同伴",
            }],
        })
        self.assertEqual(
            normalized["relationships"],
            [{"from": "林月", "to": "周宁", "relation": "同伴", "record_status": "auto"}],
        )

    def test_import_detection_groups_common_episode_suffixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [
                root / "长篇小说_01.txt",
                root / "长篇小说_02.txt",
                root / "长篇小说_03.txt",
            ]
            for path in paths:
                path.write_text("正文", encoding="utf-8")
            with patch.object(project_manager, "PROJECTS_DIR", root / "projects"):
                groups = project_manager.detect_import_groups(paths)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["name"], "长篇小说")
        self.assertEqual(
            [groups[0]["episodes"][str(path)] for path in paths],
            [1, 2, 3],
        )

    def test_existing_project_is_suggested_for_later_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            later = source / "银河物语_06.txt"
            later.write_text("正文", encoding="utf-8")
            with patch.object(project_manager, "PROJECTS_DIR", root / "projects"):
                created = project_manager.create_project(
                    "银河物语",
                    source_directories=[source],
                )
                groups = project_manager.detect_import_groups([later])

        self.assertEqual(len(groups), 1)
        self.assertEqual(
            groups[0]["existing_project"]["project_id"],
            created["project_id"],
        )
        self.assertEqual(groups[0]["episodes"][str(later)], 6)

    def test_confirmed_character_profile_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(project_manager, "PROJECTS_DIR", root / "projects"):
                project = project_manager.create_project("人物锁测试")
                project_id = project["project_id"]
                project_manager.merge_character_profiles(
                    project_id,
                    {
                        "enabled": True,
                        "characters": [{
                            "name": "林月",
                            "aliases": ["月月"],
                            "hair": "black",
                            "record_status": "confirmed",
                        }],
                    },
                )
                merged = project_manager.merge_character_profiles(
                    project_id,
                    {
                        "enabled": True,
                        "characters": [{
                            "name": "月月",
                            "hair": "red",
                            "eye_color": "brown",
                        }],
                    },
                )
                registry = json.loads(
                    (project_manager.project_dir(project_id) / "name_registry.json").read_text(
                        encoding="utf-8"
                    )
                )

        character = merged["characters"][0]
        self.assertEqual(character["hair"], "black")
        self.assertNotIn("eye_color", character)
        self.assertEqual(registry["names"][0]["name"], "林月")

    def test_character_reference_is_saved_once_in_project_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(project_manager, "PROJECTS_DIR", root / "projects"):
                project = project_manager.create_project("共享人设图")
                project_id = project["project_id"]
                analysis = {
                    "enabled": True,
                    "visual_theme": {},
                    "protagonists": ["林月"],
                    "characters": [{
                        "name": "林月",
                        "trigger": "lin_yue",
                        "importance": "protagonist",
                        "visual_prompt_en": "adult woman, black hair, blue coat",
                        "reference_prompt_en": "full body adult woman, black hair, blue coat",
                    }],
                }
                paths = []
                with patch.dict(
                    pipeline_runner.config._data,
                    {
                        "ai_api_enabled": False,
                        "character_reference_enabled": True,
                        "character_reference_provider": "same_as_image",
                        "image_provider": "placeholder",
                    },
                ):
                    for number in (1, 2):
                        job_dir = root / f"job_{number}"
                        pipeline_runner.write_status(job_dir, project_id=project_id)
                        result = pipeline_runner.stage_character_references(
                            json.loads(json.dumps(analysis)),
                            job_dir,
                        )
                        paths.append(Path(result["characters"][0]["reference_image"]))

        self.assertEqual(paths[0], paths[1])
        self.assertIn(project_id, paths[0].parts)

    def test_legacy_series_migration_copies_reference_without_deleting_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs"
            jobs.mkdir()
            reference = jobs / "old_1" / "characters" / "hero.png"
            reference.parent.mkdir(parents=True)
            Image.new("RGB", (32, 32), "blue").save(reference)
            for number in (1, 2):
                job_dir = jobs / f"old_{number}"
                job_dir.mkdir(parents=True, exist_ok=True)
                (job_dir / "status.json").write_text(
                    json.dumps({
                        "job_id": job_dir.name,
                        "series_group_key": "legacy-group",
                        "series_title": "旧系列",
                        "series_episode": number,
                        "source_directory": str(root / "source"),
                    }),
                    encoding="utf-8",
                )
            (jobs / ".series_character_profiles.json").write_text(
                json.dumps({
                    "legacy-group": {
                        "enabled": True,
                        "characters": [{
                            "name": "主角",
                            "trigger": "hero",
                            "reference_image": str(reference),
                        }],
                    }
                }),
                encoding="utf-8",
            )

            def write_status(job_dir, **updates):
                path = job_dir / "status.json"
                current = json.loads(path.read_text(encoding="utf-8"))
                current.update(updates)
                path.write_text(json.dumps(current), encoding="utf-8")

            with patch.object(project_manager, "PROJECTS_DIR", root / "projects"), patch.object(
                project_manager, "JOBS_DIR", jobs
            ):
                created = project_manager.migrate_legacy_series(
                    write_job_status=write_status
                )
                migrated_profile = project_manager.load_character_profiles(
                    created[0]["project_id"]
                )
                migrated_reference = Path(
                    migrated_profile["characters"][0]["reference_image"]
                )
                reference_copied = migrated_reference.is_file()
                job_preserved = (jobs / "old_1").exists()

        self.assertEqual(len(created), 1)
        self.assertTrue(reference_copied)
        self.assertTrue(job_preserved)


class PronunciationDictionaryRegressionTests(unittest.TestCase):
    def test_uploaded_dictionary_prepares_voicevox_text_without_mutating_subtitles(self):
        segments = [pipeline_runner.Segment(index=0, text="董卓と呂布が対峙した。")]
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            (job_dir / pipeline_runner.TTS_PRONUNCIATION_DICTIONARY).write_text(
                "董卓=とうたく\n呂布=りょふ\n",
                encoding="utf-8",
            )
            narration, counts, entries, dictionary_hash = pipeline_runner._prepare_tts_pronunciation(
                segments,
                job_dir,
                "voicevox",
            )
            subtitle = job_dir / "subtitle.srt"
            pipeline_runner.build_srt([(0.0, 2.0, segments[0].text)], subtitle)

            self.assertEqual(narration, ["とうたくとりょふが対峙した。"])
            self.assertEqual(counts, [2])
            self.assertEqual(len(entries), 2)
            self.assertTrue(dictionary_hash)
            self.assertEqual(segments[0].text, "董卓と呂布が対峙した。")
            self.assertIn("董卓と呂布", subtitle.read_text(encoding="utf-8"))
            self.assertNotIn("とうたく", subtitle.read_text(encoding="utf-8"))

    def test_uploaded_dictionary_stays_disabled_for_unrelated_tts_providers(self):
        segments = [pipeline_runner.Segment(index=0, text="董卓")]
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            (job_dir / pipeline_runner.TTS_PRONUNCIATION_DICTIONARY).write_text("董卓=とうたく\n", encoding="utf-8")
            narration, counts, entries, dictionary_hash = pipeline_runner._prepare_tts_pronunciation(
                segments,
                job_dir,
                "openai",
            )

        self.assertEqual(narration, ["董卓"])
        self.assertEqual(counts, [0])
        self.assertEqual(entries, [])
        self.assertEqual(dictionary_hash, "")


class LocalSourceSafetyRegressionTests(unittest.TestCase):
    def test_missing_local_txt_never_reaches_network_scraper(self):
        with patch.object(pipeline_runner, "_build_scraper") as build_scraper:
            with self.assertRaisesRegex(FileNotFoundError, "不会把本地路径交给"):
                pipeline_runner.stage_scrape(
                    "/tmp/definitely-missing-novel-input.txt",
                    site="qingtian",
                )
        build_scraper.assert_not_called()

    def test_moved_local_source_is_relinked_and_snapshotted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = root / "job"
            original = root / "library" / "716" / "story" / "story_01.txt"
            moved = root / "library" / "story" / original.name
            original.parent.mkdir(parents=True)
            moved.parent.mkdir(parents=True, exist_ok=True)
            job_dir.mkdir()
            original.write_text("これは正しい小説本文です。", encoding="utf-8")
            pipeline_runner.write_status(
                job_dir,
                input=str(original),
                source_path=str(original),
                source_kind="local_text",
            )
            pipeline_runner._resolve_job_input_source(job_dir, str(original))
            original.replace(moved)

            resolved = Path(pipeline_runner._resolve_job_input_source(job_dir, str(original)))
            status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))

            self.assertEqual(resolved, moved.resolve())
            self.assertEqual(Path(status["source_path"]), moved.resolve())
            self.assertEqual(
                Path(status["source_snapshot_path"]).read_text(encoding="utf-8"),
                "これは正しい小説本文です。",
            )

    def test_local_task_rejects_cached_network_novel(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            pipeline_runner.write_status(
                job_dir,
                source_path="/tmp/story.txt",
                source_kind="local_text",
            )
            wrong = pipeline_runner.Novel(
                site="qingtian",
                novel_id="wrong-book",
                title="unrelated",
                author="",
                description="",
                chapters=[
                    pipeline_runner.NovelChapter(index=1, title="chapter", text="別の本"),
                ],
            )
            with self.assertRaisesRegex(RuntimeError, "正文来源不一致"):
                pipeline_runner._validate_job_novel_source(job_dir, wrong)


class NarrationPunctuationRegressionTests(unittest.TestCase):
    def test_voicevox_preserves_authored_japanese_punctuation_and_line_endings(self):
        source = "「待って……！」\n彼女は――静かに、笑った〜。\n本当に？　はい！\n余韻—"
        with patch.dict(pipeline_runner.config._data, {"tts_provider": "voicevox"}):
            cleaned = pipeline_runner._clean_rewritten_narration_text(source)

        self.assertEqual(
            cleaned,
            source,
        )

    def test_voicevox_preserves_repeated_and_single_dashes(self):
        source = "彼女は---言葉を失った。単独の—は残す。"
        with patch.dict(pipeline_runner.config._data, {"tts_provider": "voicevox"}):
            cleaned = pipeline_runner._clean_rewritten_narration_text(source)
        self.assertEqual(cleaned, source)

    def test_edge_keeps_aggressive_punctuation_normalization(self):
        source = "「待って……！」彼女は――静かに、笑った〜。"
        with patch.dict(pipeline_runner.config._data, {"tts_provider": "edge"}):
            cleaned = pipeline_runner._clean_rewritten_narration_text(source)

        self.assertNotIn("「", cleaned)
        self.assertNotIn("」", cleaned)
        self.assertNotIn("――", cleaned)
        self.assertNotIn("〜", cleaned)
        self.assertIn("。", cleaned)
        self.assertIn("，", cleaned)


class WorkerQueueRegressionTests(unittest.TestCase):
    def test_capacity_limit_queues_without_raising_or_launching(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs_dir = Path(tmp) / "jobs"
            with (
                patch.object(pipeline_runner, "JOBS_DIR", jobs_dir),
                patch.object(pipeline_runner, "count_running_workers", return_value=1),
                patch.object(pipeline_runner.subprocess, "Popen") as popen,
            ):
                job_id, pid = pipeline_runner.start_worker(
                    "saved input",
                    job_id="waiting-job",
                    resume=True,
                    compose_only=True,
                )

            status = json.loads((jobs_dir / job_id / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(pid, 0)
            self.assertEqual(status["stage"], "queued")
            self.assertEqual(status["input"], "saved input")
            self.assertTrue(status["queued_compose_only"])
            popen.assert_not_called()

    def test_auto_start_preserves_queued_compose_only_mode(self):
        queued_status = {
            "job_id": "waiting-job",
            "stage": "queued",
            "input": "saved input",
            "worker_alive": False,
            "queued_compose_only": True,
        }
        with (
            patch.object(pipeline_runner, "count_running_workers", return_value=0),
            patch.object(pipeline_runner, "list_jobs", return_value=[queued_status]),
            patch.object(pipeline_runner, "load_status", return_value=queued_status),
            patch.object(pipeline_runner, "start_worker", return_value=("waiting-job", 1234)) as start,
        ):
            self.assertEqual(pipeline_runner.start_next_queued_job(), ("waiting-job", 1234))

        start.assert_called_once_with(
            "saved input",
            job_id="waiting-job",
            resume=True,
            compose_only=True,
            marketing_cover_only=False,
            preprocess_only=False,
        )


class MarketingCandidateRegressionTests(unittest.TestCase):
    def test_three_kingdoms_fallback_tags_never_use_isekai_romance_defaults(self):
        novel = SimpleNamespace(
            title="三国群英志第19期",
            full_text="曹操と呂布が兗州で対峙し、陳宮が計略を巡らせる。",
        )
        bundle = pipeline_runner._fallback_marketing_candidates(novel, novel.full_text, 70)
        self.assertIn("#三国志", bundle["tags"])
        self.assertNotIn("#異世界", bundle["tags"])
        self.assertNotIn("#恋愛", bundle["tags"])

    def test_upload_repairs_legacy_three_kingdoms_wrong_tags(self):
        tags = pipeline_runner._safe_generated_tags_for_upload(
            ["#小説", "#異世界", "#恋愛", "#ざまぁ", "#貴族令嬢"],
            "三国志完全解説第19席", "曹操と呂布の戦い",
        )
        self.assertIn("#三国志", tags)
        self.assertNotIn("#異世界", tags)
        self.assertNotIn("#恋愛", tags)

    def test_structured_candidates_preserve_ad_punctuation(self):
        payload = {
            "titles": [
                "愛さないと言った旦那様、私が姿を消した途端に今さら必死で探し始めました……",
                "妹を選んだ侯爵に捨てられた妻、真実が判明したら溺愛されてももう戻りません（笑）",
                "【ざまぁ】悪女扱いされた私が屋敷を去った翌日、偽りを信じた夫の人生が狂い始める",
            ],
            "synopses": [
                "妹を愛していると夫から突き放された妻。悪女の噂を信じる侯爵に見切りをつけ、真実を知る侍女と屋敷を去るが、彼女の不在をきっかけに隠されていた過去の事実が静かに動き始める……",
                "聖女と呼ばれる妹ばかりを信じ、妻を拒絶した侯爵。しかし地味な令嬢と忠実な侍女が姿を消した後、治癒をめぐる過去と社交界に広がった噂に思わぬ大きな綻びが生まれていく――",
            ],
            "tags": [
                "#恋愛", "#ざまぁ", "#婚約", "#令嬢", "#後悔",
                "#聖女", "#秘密", "#逆転", "#女性向け", "#ノベル",
            ],
        }
        bundle = pipeline_runner._parse_marketing_candidates(payload)
        self.assertIn("……", bundle["titles"][0])
        self.assertIn("（笑）", bundle["titles"][1])
        self.assertIn("【ざまぁ】", bundle["titles"][2])
        self.assertEqual(len(bundle["tags"]), 10)
        self.assertNotIn("【朗読・小説】", bundle["tag_line"])
        self.assertEqual(pipeline_runner._marketing_validation_error(bundle, 35, 70), "")

    def test_internal_source_labels_can_never_become_title_candidates(self):
        bundle = pipeline_runner._parse_marketing_candidates({
            "titles": [
                "标题：三国群英志30-35 简介：local text input [开头] 第30話。",
                "董卓は死んだ後も王允の政変は終わらず、呂布を巡る戦局が再び大きく動き始める",
                "曹操が虎牢関で敗走する一方、孫堅は井戸から伝国の玉璽を見つけてしまう",
            ],
            "synopses": ["a" * 80, "b" * 80],
            "tags": [f"#tag{i}" for i in range(10)],
        })
        self.assertEqual(len(bundle["titles"]), 2)
        self.assertFalse(pipeline_runner._metadata_has_valid_marketing_titles({
            "titles": [
                "标题：三国群英志30-35 简介：local text input [开头] 第30話。",
                "董卓は死んだ後も王允の政変は終わらず、呂布を巡る戦局が再び大きく動き始める",
                "曹操が虎牢関で敗走する一方、孫堅は井戸から伝国の玉璽を見つけてしまう",
            ]
        }))

    def test_upload_template_gets_generated_tags_only_when_they_fit(self):
        context = {
            "candidate_title": "曹操が七宝刀を手に董卓へ迫るも暗殺に失敗し、陳宮と逃亡を始める",
            "short_title": "曹操が七宝刀を手に董卓へ迫るも暗殺に失敗し、陳宮と逃亡を始める",
            "tags": "",
        }
        rendered = pipeline_runner._format_template("{candidate_title}{tags}", context)
        upload_title = pipeline_runner._append_generated_tags_to_upload_title(
            rendered,
            ["#三国志", "#歴史物語", "【朗読・小説】", "#朗読・小説", "#赤陽の勧めるノベル"],
            100,
        )
        self.assertNotIn("【朗読・小説】", upload_title)
        self.assertNotIn("赤陽の勧めるノベル", upload_title)
        self.assertIn("#三国志", upload_title)
        self.assertIn("#歴史物語", upload_title)
        self.assertLessEqual(len(upload_title), 100)

    def test_retired_channel_tag_is_removed_from_template_upload_text(self):
        self.assertEqual(
            pipeline_runner._remove_disallowed_upload_tag("説明 #赤陽の勧めるノベル #三国志"),
            "説明 #三国志",
        )

    def test_cover_prompt_is_not_clipped_at_old_1400_character_limit(self):
        prompt = pipeline_runner._normalize_cover_prompt("event and typography " * 100)
        prompt = pipeline_runner._policy_safe_image_prompt(prompt, allow_title_text=True)
        self.assertGreater(len(prompt), 1400)
        self.assertLessEqual(len(prompt), 6000)
        self.assertNotIn("1280x720", prompt)
        self.assertNotIn("3:2", prompt)


    def test_only_policy_errors_request_a_new_prompt(self):
        self.assertEqual(
            pipeline_runner._cover_error_kind("HTTP 400: content policy violation: prompt blocked"),
            "policy",
        )
        self.assertEqual(pipeline_runner._cover_error_kind("HTTP 503: service unavailable"), "transient")
        self.assertEqual(pipeline_runner._cover_error_kind("unknown model"), "other")

    def test_incomplete_cover_planner_output_is_rejected_before_image_generation(self):
        short_prompt = (
            "Premium Three Kingdoms key visual. Two commanders face each other in a dusty city. "
            "Use dramatic lighting and a wide composition."
        )
        error = pipeline_runner._cover_planner_quality_error(short_prompt)
        self.assertIn("too short", error)
        self.assertIn("no exact Japanese poster copy", error)
        self.assertIn("no headline layout", error)

    def test_source_wrapper_text_is_not_accepted_as_a_marketing_title(self):
        bundle = {
            "titles": [
                "标题：三国群英志20-24 简介：local text input [开头] 第20話の運命が動き出す瞬間です",
                "関羽が曹操に刃を向けた日、義を選ぶか野望を選ぶか天下を分ける決断が始まった",
                "張遼の一言が太師府の兵を止めたとき、怪物・呂布との圧倒的な武力差が明らかになる",
            ],
            "synopses": ["あ" * 90, "い" * 90],
            "tags": [f"#タグ{i}" for i in range(10)],
        }
        error = pipeline_runner._marketing_validation_error(bundle, 40, 70)
        self.assertIn("source-wrapper text", error)


class CoverCanvasRegressionTests(unittest.TestCase):
    def test_provider_source_is_preserved_before_cover_adaptation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "provider.png"
            backend = ImageBackend(provider="openai", model="gpt-image-1", preserve_source=True)
            provider_image = Image.new("RGB", (1536, 1024), (20, 30, 40))
            with patch.object(backend, "_post_openai_image", return_value=provider_image):
                backend.generate("cover", "", output, width=1280, height=720)
            with Image.open(output) as saved:
                self.assertEqual(saved.size, (1536, 1024))

    def test_provider_cover_is_normalized_to_exact_16_9_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.png"
            adapted = Path(tmp) / "adapted.png"
            image = Image.new("RGB", (1536, 1024), (0, 220, 0))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 1535, 79), fill=(255, 0, 0))
            draw.rectangle((0, 944, 1535, 1023), fill=(0, 0, 255))
            image.save(source)

            pipeline_runner._fit_provider_cover_to_16_9(source, adapted, 1280, 720)

            with Image.open(adapted).convert("RGB") as result:
                self.assertEqual(result.size, (1280, 720))
                top = result.getpixel((640, 8))
                bottom = result.getpixel((640, 711))
                left = result.getpixel((8, 360))
                right = result.getpixel((1271, 360))
            for pixel in (top, bottom, left, right):
                self.assertGreater(pixel[1], 170)
                self.assertLess(pixel[0], 70)
                self.assertLess(pixel[2], 70)


class FullRetryRegressionTests(unittest.TestCase):
    def test_full_retry_clears_generated_files_but_preserves_user_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs"
            job = jobs / "sample"
            source = root / "source" / "novel.txt"
            source.parent.mkdir(parents=True)
            source.write_text("正しい原文", encoding="utf-8")
            (job / "images").mkdir(parents=True)
            (job / "audio").mkdir()
            (job / "images" / "old.png").write_bytes(b"old")
            (job / "audio" / pipeline_runner.IMPORTED_AUDIO_FILENAME).write_bytes(b"mp3")
            (job / "audio" / "seg_00000.mp3").write_bytes(b"generated")
            (job / pipeline_runner.IMPORTED_AUDIO_MANIFEST).write_text(
                json.dumps({"path": f"audio/{pipeline_runner.IMPORTED_AUDIO_FILENAME}"}), encoding="utf-8"
            )
            (job / pipeline_runner.TTS_PRONUNCIATION_DICTIONARY).write_text("吕布=りょふ", encoding="utf-8")
            (job / "settings_snapshot.json").write_text("{}", encoding="utf-8")
            (job / "prompts.json").write_text("[]", encoding="utf-8")
            (job / "marketing_candidates.json").write_text("{}", encoding="utf-8")
            (job / "タイトル・あらすじ候補.txt").write_text("新候補", encoding="utf-8")
            (job / "タイトル・あらすじ・タグ候補.txt").write_text("候補", encoding="utf-8")
            (job / "upload_title_selection.json").write_text("{}", encoding="utf-8")
            (job / "status.json").write_text(
                json.dumps({
                    "job_id": "sample",
                    "input": str(source),
                    "source_path": str(source),
                    "source_kind": "local_text",
                }),
                encoding="utf-8",
            )

            with patch.object(pipeline_runner, "JOBS_DIR", jobs):
                pipeline_runner.reset_full_job("sample")

            self.assertFalse((job / "images").exists())
            self.assertFalse((job / "prompts.json").exists())
            self.assertFalse((job / "settings_snapshot.json").exists())
            self.assertFalse((job / "marketing_candidates.json").exists())
            self.assertFalse((job / "タイトル・あらすじ候補.txt").exists())
            self.assertFalse((job / "タイトル・あらすじ・タグ候補.txt").exists())
            self.assertFalse((job / "upload_title_selection.json").exists())
            self.assertFalse((job / "audio" / "seg_00000.mp3").exists())
            self.assertTrue((job / "audio" / pipeline_runner.IMPORTED_AUDIO_FILENAME).exists())
            self.assertTrue((job / pipeline_runner.IMPORTED_AUDIO_MANIFEST).exists())
            self.assertTrue((job / pipeline_runner.TTS_PRONUNCIATION_DICTIONARY).exists())
            status = json.loads((job / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["input"], str(source.resolve()))
            self.assertEqual(status["stage"], "queued")


class ManualCoverRegenerationTests(unittest.TestCase):
    def test_regenerates_only_cover_and_restores_completed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp)
            job = jobs / "sample"
            (job / "cover").mkdir(parents=True)
            (job / "cover" / "cover.jpg").write_bytes(b"old-cover")
            (job / "audio").mkdir()
            (job / "audio" / "seg_00000.mp3").write_bytes(b"audio-kept")
            (job / "images").mkdir()
            (job / "images" / "img_00000.png").write_bytes(b"image-kept")
            (job / "final.mp4").write_bytes(b"video-kept")
            (job / "novel.json").write_text(
                json.dumps({
                    "site": "text",
                    "id": "sample",
                    "title": "Title",
                    "chapters": [{"index": 1, "title": "", "text": "Story text"}],
                }),
                encoding="utf-8",
            )
            (job / "segments.json").write_text(
                json.dumps([{"i": 0, "text": "Story text"}]), encoding="utf-8"
            )
            (job / "metadata.json").write_text("{}", encoding="utf-8")
            (job / "status.json").write_text(
                json.dumps({"job_id": "sample", "stage": "completed", "progress": 1.0}), encoding="utf-8"
            )

            def fake_stage_cover(_novel, _segments, job_dir, **kwargs):
                self.assertTrue(kwargs["force"])
                self.assertEqual(len(kwargs["metadata"]["titles"]), 3)
                self.assertEqual(len(kwargs["metadata"]["synopses"]), 2)
                cover = job_dir / "cover" / "cover.jpg"
                cover.parent.mkdir(parents=True)
                cover.write_bytes(b"new-cover")
                return cover

            generated_metadata = {
                "titles": ["候補タイトル一", "候補タイトル二", "候補タイトル三"],
                "synopses": ["第一のあらすじ", "第二のあらすじ"],
                "generated_tags": ["#三国志"],
            }

            with (
                patch.object(pipeline_runner, "JOBS_DIR", jobs),
                patch.object(pipeline_runner, "is_worker_running", return_value=False),
                patch.object(pipeline_runner, "stage_metadata", return_value=generated_metadata) as metadata_stage,
                patch.object(pipeline_runner, "stage_cover", side_effect=fake_stage_cover),
            ):
                cover = pipeline_runner.regenerate_job_cover("sample")

            metadata_stage.assert_called_once()
            self.assertEqual(cover.read_bytes(), b"new-cover")
            self.assertEqual((job / "audio" / "seg_00000.mp3").read_bytes(), b"audio-kept")
            self.assertEqual((job / "images" / "img_00000.png").read_bytes(), b"image-kept")
            self.assertEqual((job / "final.mp4").read_bytes(), b"video-kept")
            status = json.loads((job / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["stage"], "completed")
            self.assertEqual(status["progress"], 1.0)
            self.assertEqual(status["cover"], str(cover))


class BrowserUploadProfileRegressionTests(unittest.TestCase):
    def test_single_channel_upload_forces_selected_chrome_profile_launch(self):
        profile = {
            "name": "三国 千夜一席",
            "enabled": True,
            "chrome_profile": "Account-2",
            "flow": "simple",
            "visibility": "PRIVATE",
        }
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job"
            job_dir.mkdir()
            video = job_dir / "video.mp4"
            video.write_bytes(b"video")
            lock_path = job_dir / ".upload.lock"
            with (
                patch.object(pipeline_runner, "_selected_upload_profiles", return_value=[profile]),
                patch.object(pipeline_runner, "_ensure_upload_dependencies"),
                patch.object(pipeline_runner, "_acquire_upload_lock", return_value=lock_path),
                patch.object(pipeline_runner, "_release_upload_lock"),
                patch("app.upload.upload_to_youtube", return_value="video-id") as upload_mock,
            ):
                pipeline_runner.stage_upload(
                    video,
                    "测试标题",
                    None,
                    job_dir,
                    force=True,
                    schedule_enabled_override=False,
                )

        self.assertEqual(upload_mock.call_args.kwargs["chrome_profile"], "Account-2")
        self.assertTrue(upload_mock.call_args.kwargs["force_profile_launch"])


class ShareableProfileRegressionTests(unittest.TestCase):
    def test_builtin_japanese_profiles_omit_connection_details_and_preserve_them_on_load(self):
        profile_names = ("日语推文·二次元画风", "日语推文·真人画风")
        profile_dir = Path(__file__).parents[1] / "data" / "profiles"
        for name in profile_names:
            data = json.loads((profile_dir / f"{name}.json").read_text(encoding="utf-8"))
            self.assertFalse(set(data) & config_module.PROFILE_CONNECTION_FIELDS)

        with tempfile.TemporaryDirectory() as tmp:
            profile_copy = Path(tmp) / "profiles"
            profile_copy.mkdir()
            for name in profile_names:
                source = profile_dir / f"{name}.json"
                (profile_copy / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            with patch.object(config_module, "PROFILES_DIR", profile_copy):
                isolated = config_module.Config()
                isolated.set("ai_api_base_url", "https://example.invalid/v1")
                isolated.set("ai_api_key", "test-key")
                isolated.set("image_base_url", "https://images.example.invalid/v1")
                isolated.load_profile("日语推文·二次元画风")
                self.assertEqual(isolated.get("ai_api_base_url"), "https://example.invalid/v1")
                self.assertEqual(isolated.get("ai_api_key"), "test-key")
                self.assertEqual(isolated.get("image_base_url"), "https://images.example.invalid/v1")


if __name__ == "__main__":
    unittest.main()
