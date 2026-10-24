"""Exercise AstrBot handler contracts with stubs; no full AstrBot install needed."""

import importlib.util
import json
import sys
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_screenshot.engine import CaptureError, Snapshot

ROOT = Path(__file__).resolve().parents[1]


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    @classmethod
    def fromFileSystem(cls, path):
        return SimpleNamespace(path=path)


class Star:
    def __init__(self, context):
        self.context = context


def decorator(*args, **kwargs):
    def wrap(fn):
        fn.filter_options = (args, kwargs)
        return fn

    return wrap


class Event:
    def __init__(self, text="https://example.com/", *, sender_id="1", self_id="bot"):
        self.sender_id = sender_id
        self.self_id = self_id
        self.message_str = text
        self.unified_msg_origin = "test:GroupMessage:1"
        self.is_at_or_wake_command = False
        self._has_send_oper = False
        self.stopped = False
        self.extra = {}
        self.sent = []
        self.files = []

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def get_sender_id(self):
        return self.sender_id

    def get_self_id(self):
        return self.self_id

    def get_result(self):
        return None

    def is_stopped(self):
        return self.stopped

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return [Plain(text)]

    def chain_result(self, chain):
        return chain

    async def send(self, chain):
        for item in chain:
            if hasattr(item, "path"):
                assert Path(item.path).is_file(), "Temp file was deleted before awaited send"
                self.files.append(Path(item.path))
        self.sent.append(chain)
        self._has_send_oper = True


def load_plugin(data_path):
    modules = {}
    for name in (
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.message_components",
        "astrbot.api.star",
        "astrbot.core",
        "astrbot.core.utils",
        "astrbot.core.utils.astrbot_path",
    ):
        modules[name] = ModuleType(name)
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = SimpleNamespace(warning=lambda *args: None)
    modules["astrbot.api.event"].AstrMessageEvent = Event
    modules["astrbot.api.event"].filter = SimpleNamespace(
        command=decorator,
        event_message_type=decorator,
        EventMessageType=SimpleNamespace(ALL="all"),
    )
    modules["astrbot.api.message_components"].Image = Image
    modules["astrbot.api.message_components"].Plain = Plain
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = lambda: data_path
    name = "astrbot_plugin_screenshot._test_main"
    spec = importlib.util.spec_from_file_location(name, ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module.ScreenshotPlugin


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        cls = load_plugin(self.temp.name)
        self.plugin = cls(None, {"cooldown": 0})

        async def capture(url, directory):
            path = directory / "01.png"
            path.write_bytes(b"fake fixture image")
            return Snapshot([path], "test snapshot")

        self.plugin.engine.capture = AsyncMock(side_effect=capture)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_low_priority(self):
        self.assertEqual(self.plugin.fallback.filter_options[1]["priority"], -10000)

    async def test_fallback_success_and_cleanup(self):
        event = Event()
        await self.plugin.fallback(event)
        self.plugin.engine.capture.assert_awaited_once()
        self.assertTrue(event.stopped)
        self.assertTrue(event.extra["screenshot_handled"])
        self.assertTrue(event.files)
        self.assertTrue(all(not path.exists() for path in event.files))

    async def test_self_messages_ignored_at_both_entry_points(self):
        for handler, text in (
            (self.plugin.fallback, "https://example.com/"),
            (self.plugin.screenshot, "/截图 https://example.com/"),
            (self.plugin.screenshot, "/截图 帮助"),
        ):
            for sender_id, self_id in (("bot", "bot"), (123, "123"), ("123", 123)):
                with self.subTest(handler=handler.__name__, text=text, ids=(sender_id, self_id)):
                    self.plugin.reservations = type(self.plugin.reservations)(self.plugin.settings)
                    self.plugin.engine.capture.reset_mock()
                    event = Event(text, sender_id=sender_id, self_id=self_id)
                    await handler(event)
                    self.plugin.engine.capture.assert_not_awaited()
                    self.assertFalse(event.sent)
                    self.assertFalse(event.stopped)
                    self.assertEqual(event.extra, {})

    async def test_non_self_or_unknown_ids_not_suppressed(self):
        for handler, text in (
            (self.plugin.fallback, "https://example.com/"),
            (self.plugin.screenshot, "/截图 https://example.com/"),
        ):
            for sender_id, self_id in (
                ("user", "bot"),
                ("", ""),
                (None, None),
                ("user", ""),
                ("", "bot"),
            ):
                with self.subTest(handler=handler.__name__, ids=(sender_id, self_id)):
                    self.plugin.reservations = type(self.plugin.reservations)(self.plugin.settings)
                    self.plugin.engine.capture.reset_mock()
                    event = Event(text, sender_id=sender_id, self_id=self_id)
                    await handler(event)
                    self.plugin.engine.capture.assert_awaited_once()
                    self.assertTrue(event.sent)

    async def test_previous_response_not_repeated(self):
        for sent in (True, False):
            event = Event()
            event._has_send_oper = sent
            event.extra["url_parser_handled"] = not sent
            await self.plugin.fallback(event)
        self.plugin.engine.capture.assert_not_awaited()

    async def test_skip_manual_override(self):
        cls = load_plugin(self.temp.name)
        self.plugin.settings = cls(None, {"skip_domains": ["example.com"]}).settings
        event = Event()
        await self.plugin.fallback(event)
        self.plugin.engine.capture.assert_not_awaited()
        event.message_str = "/截图 https://example.com/"
        await self.plugin.screenshot(event)
        self.plugin.engine.capture.assert_awaited_once()

    async def test_manual_error_stops_but_auto_failure_does_not(self):
        self.plugin.engine.capture.side_effect = CaptureError("safe error")
        event = Event()
        await self.plugin.fallback(event)
        self.assertFalse(event.stopped)
        self.assertFalse(event.sent)
        event = Event("/截图 https://example.com/")
        await self.plugin.screenshot(event)
        self.assertTrue(event.stopped)
        self.assertEqual(event.sent[0][0].text, "safe error")
        self.assertEqual(list(self.plugin.root.iterdir()), [])

    async def test_unrelated_command_not_captured(self):
        await self.plugin.fallback(Event("/other https://example.com/"))
        self.plugin.engine.capture.assert_not_awaited()

    async def test_session_and_mention_filters(self):
        from dataclasses import replace

        self.plugin.settings = replace(self.plugin.settings, require_at=True)
        await self.plugin.fallback(Event())
        self.plugin.engine.capture.assert_not_awaited()
        self.plugin.settings = replace(
            self.plugin.settings, require_at=False, allowed_sessions=("other",)
        )
        await self.plugin.fallback(Event())
        self.plugin.engine.capture.assert_not_awaited()

    async def test_two_links_in_one_event(self):
        from dataclasses import replace

        self.plugin.settings = replace(self.plugin.settings, max_links=2, cooldown=15)
        self.plugin.reservations.settings = self.plugin.settings
        await self.plugin.fallback(Event("https://a.example/ https://b.example/"))
        self.assertEqual(self.plugin.engine.capture.await_count, 2)

    async def test_dependency_manifests_match(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(requirements, project["dependencies"])
        self.assertIn(
            "version: v" + project["version"], (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        )

    async def test_schema_defaults_match_settings(self):
        from astrbot_plugin_screenshot.config import Settings

        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            Settings.load({key: item["default"] for key, item in schema.items()}), Settings()
        )
        self.assertEqual(
            set(schema) - {"routes_json"}, set(Settings.__dataclass_fields__) - {"routes"}
        )


if __name__ == "__main__":
    unittest.main()
