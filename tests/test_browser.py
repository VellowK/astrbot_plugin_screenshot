"""Opt-in local Chromium/Edge tests. No NodeSeek network access required."""

import asyncio
import logging
import os
import struct
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_screenshot.browser import BrowserRuntime
from astrbot_plugin_screenshot.config import Settings
from astrbot_plugin_screenshot.engine import CaptureError, ScreenshotEngine, nodeseek_html

ROOT = Path(__file__).resolve().parents[1]
BROWSER = os.environ.get("SCREENSHOT_TEST_BROWSER", "")


@unittest.skipUnless(BROWSER, "Set SCREENSHOT_TEST_BROWSER to chromium or an executable path")
class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        settings = Settings(
            executable_path="" if BROWSER == "chromium" else BROWSER,
            auto_install_browser=False,  # Opt-in rendering tests still never download.
            browser_idle_seconds=0,
        )
        self.config = {}
        self.runtime = BrowserRuntime(settings, self.config, logger=logging.getLogger(__name__))
        self.addAsyncCleanup(self.runtime.close)
        await self.runtime.start()
        await self.runtime._setup_task
        self.assertTrue(self.runtime.ready, self.runtime.status)
        self.assertTrue(self.config["browser_ready"])
        self.browser = self.runtime._browser

    async def test_adapter_extract_and_sanitize(self):
        page = await self.browser.new_page()
        await page.route("**/*", lambda route: route.abort())
        # Use a base tag so relative URL normalization is exercised without network access.
        fixture = (ROOT / "tests/fixtures/nodeseek.html").read_text(encoding="utf-8")
        await page.set_content('<base href="https://www.nodeseek.com/">' + fixture)
        data = await page.evaluate((ROOT / "nodeseek.js").read_text(encoding="utf-8"), 10)
        self.assertEqual(data["total"], 3)
        self.assertEqual([item["floor"] for item in data["comments"]], ["1", "4", "2"])
        self.assertNotIn("onclick", data["html"])
        self.assertNotIn("<script", data["html"])
        self.assertNotIn("<iframe", data["html"])
        self.assertNotIn("javascript:", data["html"])
        card = nodeseek_html(data, "https://www.nodeseek.com/post-123-1")
        for unwanted in (
            "REMOVE_NAVIGATION",
            "REMOVE_SIDEBAR",
            "REMOVE_ORDINARY_COMMENT",
            "REMOVE_FOOTER",
        ):
            self.assertNotIn(unwanted, card)
        self.assertEqual(card.count("BOTH_COMMENT"), 1)
        small = await page.evaluate((ROOT / "nodeseek.js").read_text(encoding="utf-8"), 1)
        self.assertEqual(len(small["comments"]), 1)
        self.assertTrue(small["comments"][0]["pinned"])

    async def test_full_engine_local_fixture_and_slices(self):
        await self._full_engine_fixture(shared=False)

    async def test_shared_browser_fixture_isolation_and_gateway(self):
        await self._full_engine_fixture(shared=True)

    async def _full_engine_fixture(self, *, shared):
        # Only these tests replace outbound connections with a loopback fixture server.
        # Production resolution and routing remain strict and are covered by test_core.
        fixture = (ROOT / "tests/fixtures/nodeseek.html").read_bytes()

        async def handle(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\nContent-Length: "
                    + str(len(fixture)).encode()
                    + b"\r\n\r\n"
                    + fixture
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        connections = []

        async def fixture_connect(_gateway, _host, _port):
            connections.append((_host, _port))
            return await asyncio.open_connection("127.0.0.1", port)

        settings = replace(
            Settings(),
            executable_path="" if BROWSER == "chromium" else BROWSER,
            settle_ms=0,
            slice_height=900,
            max_height=4000,
        )

        # Leave an unrelated context alive in the owned process. A capture must
        # neither borrow its cookies nor close it, and must use its own gateway.
        existing = await self.browser.new_context()
        await existing.add_cookies(
            [{"name": "private", "value": "keep", "url": "http://www.nodeseek.com/"}]
        )
        provider = AsyncMock(return_value=self.runtime) if shared else None
        engine = ScreenshotEngine(settings, browser_provider=provider)
        try:
            with (
                TemporaryDirectory() as tmp,
                patch(
                    "astrbot_plugin_screenshot.engine.resolve_public",
                    AsyncMock(return_value=["1.1.1.1"]),
                ),
                patch("astrbot_plugin_screenshot.gateway.Gateway._connect", fixture_connect),
            ):
                snapshot = await engine.capture("http://www.nodeseek.com/post-123-9", Path(tmp))
                self.assertGreaterEqual(len(snapshot.paths), 2)
                self.assertTrue(connections, "Shared context must use our guarded gateway")
                self.assertTrue(self.browser.is_connected())
                self.assertEqual(self.browser.contexts, [existing])
                self.assertEqual((await existing.cookies())[0]["value"], "keep")
                if shared:
                    provider.assert_awaited_once()
                for image in snapshot.paths:
                    raw = image.read_bytes()
                    self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
                    width, height = struct.unpack(">II", raw[16:24])
                    self.assertEqual(width, 1080)
                    self.assertLessEqual(height, 900)
                # Persist a fixture-only preview outside version control for visual inspection.
                import shutil

                target = ROOT / ".research" / "fixture-preview.png"
                target.parent.mkdir(exist_ok=True)
                shutil.copyfile(snapshot.paths[0], target)
        finally:
            await engine.close()
            await existing.close()
            server.close()
            await server.wait_closed()

    async def test_unsafe_target_rejected(self):
        with TemporaryDirectory() as tmp:
            engine = ScreenshotEngine(Settings())
            with self.assertRaises(CaptureError):
                await engine.capture("http://127.0.0.1/", Path(tmp))
            await engine.close()


if __name__ == "__main__":
    unittest.main()
