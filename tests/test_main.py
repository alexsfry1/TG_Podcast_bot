import asyncio
import json
import os
import stat
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, NetworkError

import main


def make_entry(number: int):
    return {
        "id": str(number),
        "title": f"Episode {number}",
        "enclosures": [
            {
                "href": f"https://example.test/{number}.mp3",
                "type": "audio/mpeg",
            }
        ],
    }


class PublishProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.temp_dir.name, "config.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_config(self, **overrides):
        config = {
            "rss_url": "https://example.test/feed.xml",
            "channel": "@channel",
            "last_published_id": "0",
        }
        config.update(overrides)
        main.save_config(self.config_path, config)
        return config

    async def test_max_items_publishes_oldest_unseen_without_skipping(self):
        parsed = SimpleNamespace(
            bozo=False,
            entries=[make_entry(number) for number in range(10, -1, -1)],
            feed={},
        )
        config = self.write_config(max_items_per_run=3)
        posted = []

        async def fake_publish(*args, **kwargs):
            posted.append(args[2]["id"])

        with patch.object(main.feedparser, "parse", return_value=parsed), patch.object(
            main,
            "publish_entry",
            fake_publish,
        ):
            await main.publish_new_entries(object(), config, self.config_path)
            self.assertEqual(posted, ["1", "2", "3"])
            self.assertEqual(main.load_config(self.config_path)["last_published_id"], "3")

            posted.clear()
            config = main.load_config(self.config_path)
            await main.publish_new_entries(object(), config, self.config_path)

        self.assertEqual(posted, ["4", "5", "6"])
        self.assertEqual(main.load_config(self.config_path)["last_published_id"], "6")

    async def test_successful_entry_is_checkpointed_before_later_failure(self):
        parsed = SimpleNamespace(
            bozo=False,
            entries=[make_entry(number) for number in range(3, -1, -1)],
            feed={},
        )
        config = self.write_config()
        attempted = []

        async def fail_on_second(*args, **kwargs):
            attempted.append(args[2]["id"])
            if len(attempted) == 2:
                raise RuntimeError("simulated failure")

        with patch.object(main.feedparser, "parse", return_value=parsed), patch.object(
            main,
            "publish_entry",
            fail_on_second,
        ):
            with self.assertRaises(RuntimeError):
                await main.publish_new_entries(object(), config, self.config_path)

        self.assertEqual(attempted, ["1", "2"])
        self.assertEqual(main.load_config(self.config_path)["last_published_id"], "1")

    async def test_checkpoint_preserves_config_changes_made_during_publish(self):
        parsed = SimpleNamespace(
            bozo=False,
            entries=[make_entry(1), make_entry(0)],
            feed={},
        )
        config = self.write_config(language="ru")
        config_lock = asyncio.Lock()

        async def change_language_while_publishing(*args, **kwargs):
            async with config_lock:
                latest = main.load_config(self.config_path)
                latest["language"] = "en"
                main.save_config(self.config_path, latest)

        with patch.object(main.feedparser, "parse", return_value=parsed), patch.object(
            main,
            "publish_entry",
            change_language_while_publishing,
        ):
            await main.publish_new_entries(
                object(),
                config,
                self.config_path,
                config_lock=config_lock,
            )

        saved = main.load_config(self.config_path)
        self.assertEqual(saved["language"], "en")
        self.assertEqual(saved["last_published_id"], "1")


class FormattingTests(unittest.TestCase):
    def test_original_link_is_preserved_in_full_caption(self):
        url = "https://example.test/" + "x" * 300
        result = main.append_original_link("A" * 1024, url, "ru")
        self.assertLessEqual(len(result), 1024)
        self.assertIn(url, result)

    def test_fallback_link_is_preserved_with_long_description(self):
        url = "https://example.test/" + "x" * 300
        text = main.format_text({"title": "Title", "summary": "B" * 5000})
        result = main.build_fallback_message(text, url, "ru")
        self.assertLessEqual(len(result), main.MAX_MESSAGE_LENGTH)
        self.assertIn(url, result)


class FileSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_save_config_is_private_and_valid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "config.json")
            main.save_config(path, {"bot_token": "secret", "value": 1})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with open(path, "r", encoding="utf-8") as config_file:
                self.assertEqual(json.load(config_file)["value"], 1)
            self.assertEqual(os.listdir(temp_dir), ["config.json"])

    def test_failed_config_write_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "config.json")
            main.save_config(path, {"value": 1})
            with self.assertRaises(TypeError):
                main.save_config(path, {"value": object()})

            self.assertEqual(main.load_config(path)["value"], 1)
            self.assertEqual(os.listdir(temp_dir), ["config.json"])

    def test_failed_transcode_removes_output_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_named_temp = tempfile.NamedTemporaryFile

            def named_temp_in_test_dir(*args, **kwargs):
                kwargs["dir"] = temp_dir
                return original_named_temp(*args, **kwargs)

            error = subprocess.CalledProcessError(1, ["ffmpeg"])
            with patch.object(
                main.tempfile,
                "NamedTemporaryFile",
                side_effect=named_temp_in_test_dir,
            ), patch.object(main.subprocess, "run", side_effect=error):
                with self.assertRaises(RuntimeError):
                    main.transcode_audio("input.mp3", 64)

            self.assertEqual(os.listdir(temp_dir), [])

    async def test_oversized_transcode_is_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = os.path.join(temp_dir, "input.mp3")
            output_path = os.path.join(temp_dir, "output.mp3")
            with open(input_path, "wb") as input_file:
                input_file.write(b"input")
            with open(output_path, "wb") as output_file:
                output_file.write(b"too large")

            bot = SimpleNamespace(send_message=AsyncMock())
            entry = {
                "title": "Episode",
                "enclosures": [
                    {"href": "https://example.test/audio.mp3", "type": "audio/mpeg"}
                ],
            }
            with patch.object(main, "download_to_temp", return_value=input_path), patch.object(
                main,
                "get_audio_duration_seconds",
                return_value=60,
            ), patch.object(main, "transcode_audio", return_value=output_path):
                await main.publish_entry(
                    bot,
                    "@channel",
                    entry,
                    {},
                    max_upload_bytes=1,
                    max_source_bytes=100,
                    transcode_enabled=True,
                    send_original_link=True,
                    audio_send_mode="upload",
                    max_thumb_source_bytes=100,
                    language="ru",
                )

            self.assertFalse(os.path.exists(input_path))
            self.assertFalse(os.path.exists(output_path))
            bot.send_message.assert_awaited_once()


class TelegramSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_send_error_is_not_retried(self):
        calls = 0

        async def failing_call():
            nonlocal calls
            calls += 1
            raise NetworkError("ambiguous failure")

        with self.assertRaises(NetworkError):
            await main.call_telegram_once("test", failing_call)
        self.assertEqual(calls, 1)

    async def test_local_cover_fallback_is_treated_as_sent(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as cover_file:
            cover_path = cover_file.name

        bot = SimpleNamespace(
            send_photo=AsyncMock(
                side_effect=[
                    BadRequest("cannot fetch cover URL"),
                    SimpleNamespace(message_id=41),
                ]
            ),
            send_audio=AsyncMock(),
        )
        entry = {
            "title": "Episode",
            "image": "https://example.test/cover.jpg",
            "enclosures": [
                {"href": "https://example.test/audio.mp3", "type": "audio/mpeg"}
            ],
        }
        with patch.object(main, "build_thumbnail", return_value=cover_path):
            await main.publish_entry(
                bot,
                "@channel",
                entry,
                {},
                max_upload_bytes=100,
                max_source_bytes=100,
                transcode_enabled=False,
                send_original_link=True,
                audio_send_mode="url",
                max_thumb_source_bytes=100,
                language="ru",
            )

        self.assertEqual(bot.send_photo.await_count, 2)
        self.assertIsNone(bot.send_audio.await_args.kwargs["caption"])
        self.assertFalse(os.path.exists(cover_path))

    async def test_cover_is_deleted_when_audio_publication_fails(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as cover_file:
            cover_path = cover_file.name

        bot = SimpleNamespace(
            send_photo=AsyncMock(return_value=SimpleNamespace(message_id=42)),
            send_audio=AsyncMock(side_effect=BadRequest("fatal audio error")),
            delete_message=AsyncMock(),
        )
        entry = {
            "title": "Episode",
            "image": "https://example.test/cover.jpg",
            "enclosures": [
                {"href": "https://example.test/audio.mp3", "type": "audio/mpeg"}
            ],
        }
        with patch.object(main, "build_thumbnail", return_value=cover_path):
            with self.assertRaises(BadRequest):
                await main.publish_entry(
                    bot,
                    "@channel",
                    entry,
                    {},
                    max_upload_bytes=100,
                    max_source_bytes=100,
                    transcode_enabled=False,
                    send_original_link=True,
                    audio_send_mode="url",
                    max_thumb_source_bytes=100,
                    language="ru",
                )

        bot.delete_message.assert_awaited_once_with(chat_id="@channel", message_id=42)
        self.assertFalse(os.path.exists(cover_path))


class BackgroundProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_all_returns_while_work_continues_in_background(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.json")
            main.save_config(
                config_path,
                {
                    "admin_ids": [7],
                    "channel": "@channel",
                    "rss_url": "https://example.test/feed.xml",
                    "language": "ru",
                },
            )
            release_work = asyncio.Event()

            async def fake_publish(*args, **kwargs):
                await release_work.wait()
                return 2

            class FakeApplication:
                def __init__(self):
                    self.task = None

                def create_task(self, coroutine, **kwargs):
                    self.task = asyncio.create_task(coroutine)
                    return self.task

            application = FakeApplication()
            message = SimpleNamespace(reply_text=AsyncMock())
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=7),
                effective_message=message,
            )
            context = SimpleNamespace(
                bot=object(),
                bot_data={"config_path": config_path},
                application=application,
            )

            with patch.object(main, "publish_new_entries", fake_publish):
                await main.process_all_podcast(update, context)
                self.assertIsNotNone(application.task)
                self.assertFalse(application.task.done())
                self.assertTrue(context.bot_data[main.PUBLISH_LOCK_KEY].locked())
                release_work.set()
                await application.task

            self.assertFalse(context.bot_data[main.PUBLISH_LOCK_KEY].locked())
            self.assertGreaterEqual(message.reply_text.await_count, 2)


if __name__ == "__main__":
    unittest.main()
