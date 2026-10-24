"""Browser-independent orchestration tests; real rendering is in test_browser.py."""

import asyncio
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_screenshot.config import Settings
from astrbot_plugin_screenshot.engine import CaptureError, ScreenshotEngine


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_document_slices_and_cleanup(self):
        settings = replace(Settings(), max_height=3000, slice_height=1200, settle_ms=0)
        engine = ScreenshotEngine(settings)
        page = MagicMock()
        page.goto = AsyncMock(
            return_value=MagicMock(status=200, headers={"content-type": "text/html"})
        )
        page.title = AsyncMock(return_value="Fixture")
        page.wait_for_timeout = AsyncMock()
        page.evaluate = AsyncMock(return_value={"width": 1080, "height": 3500})
        page.screenshot = AsyncMock()
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        context.route = AsyncMock()
        context.route_web_socket = AsyncMock()
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        pw = MagicMock()
        pw.chromium.launch = AsyncMock(return_value=browser)
        playwright = MagicMock()
        playwright.__aenter__ = AsyncMock(return_value=pw)
        gateway = MagicMock(address="http://127.0.0.1:12345", exhausted=False, blocked=0)
        gateway.__aenter__ = AsyncMock(return_value=gateway)
        with (
            TemporaryDirectory() as tmp,
            patch("playwright.async_api.async_playwright", return_value=playwright),
            patch("astrbot_plugin_screenshot.engine.Gateway", return_value=gateway),
            patch(
                "astrbot_plugin_screenshot.engine.resolve_public",
                AsyncMock(return_value=["1.1.1.1"]),
            ),
            patch.object(engine, "_settle_images", AsyncMock()),
        ):
            snapshot = await engine.capture("https://example.com/", Path(tmp))
        self.assertEqual(len(snapshot.paths), 3)
        self.assertIn("底部已截断", snapshot.note)
        clips = [call.kwargs["clip"] for call in page.screenshot.await_args_list]
        self.assertEqual([clip["y"] for clip in clips], [0, 1200, 2400])
        self.assertEqual([clip["height"] for clip in clips], [1200, 1200, 600])
        # Without full_page, Playwright trims these clips to the 900px viewport.
        self.assertTrue(all(call.kwargs["full_page"] for call in page.screenshot.await_args_list))
        self.assertTrue(pw.chromium.launch.call_args.kwargs["chromium_sandbox"])
        self.assertEqual(context.new_page.await_count, 1)
        self.assertEqual(browser.new_context.call_args.kwargs["service_workers"], "block")
        browser.close.assert_awaited_once()
        self.assertFalse(engine._active)

    async def test_invalid_urls_safe_errors(self):
        engine = ScreenshotEngine(Settings())
        for url in ("file:///etc/passwd", "http://127.0.0.1/", "https://example.com:8080/"):
            with self.subTest(url=url), self.assertRaises(CaptureError):
                await engine.capture(url, Path("unused"))
        self.assertFalse(engine._active)

    async def test_close_cancels_active_and_rejects_new(self):
        engine = ScreenshotEngine(Settings())
        started = asyncio.Event()

        async def block(*_args):
            started.set()
            await asyncio.Future()

        with patch.object(engine, "_capture", side_effect=block):
            task = asyncio.create_task(engine.capture("https://example.com/", Path("unused")))
            await started.wait()
            await engine.close()
            self.assertTrue(task.cancelled())
        self.assertFalse(engine._active)
        with self.assertRaises(CaptureError):
            await engine.capture("https://example.com/", Path("unused"))


if __name__ == "__main__":
    unittest.main()
