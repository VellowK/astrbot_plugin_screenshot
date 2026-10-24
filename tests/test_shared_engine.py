"""Shared browser lifecycle and security policies use the same capture path."""

import asyncio
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_screenshot.browser import BrowserUnavailable
from astrbot_plugin_screenshot.config import Settings
from astrbot_plugin_screenshot.engine import CaptureError, ScreenshotEngine


class SharedEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.page = MagicMock()
        self.page.goto = AsyncMock(
            return_value=MagicMock(status=200, headers={"content-type": "text/html"})
        )
        self.page.title = AsyncMock(return_value="Fixture")
        self.page.wait_for_timeout = AsyncMock()
        self.page.evaluate = AsyncMock(return_value={"width": 1080, "height": 900})
        self.page.screenshot = AsyncMock()
        self.contexts = []
        self.browser = MagicMock()
        self.browser.close = AsyncMock()
        self.browser.new_context = AsyncMock(side_effect=self.new_context)
        self.in_operation = False
        self.operations = 0

        @asynccontextmanager
        async def operation():
            self.operations += 1
            self.in_operation = True
            try:
                yield self.browser
            finally:
                self.in_operation = False

        self.service = SimpleNamespace(operation=operation)
        self.provider = AsyncMock(return_value=self.service)
        self.engine = ScreenshotEngine(Settings(), browser_provider=self.provider)
        self.addAsyncCleanup(self.engine.close)
        self.gateway = MagicMock(address="http://127.0.0.1:12345", exhausted=False, blocked=0)
        self.gateway.__aenter__ = AsyncMock(return_value=self.gateway)
        self.enterContext(
            patch("astrbot_plugin_screenshot.engine.Gateway", return_value=self.gateway)
        )
        self.enterContext(
            patch(
                "astrbot_plugin_screenshot.engine.resolve_public",
                AsyncMock(return_value=["1.1.1.1"]),
            )
        )
        self.enterContext(patch.object(self.engine, "_settle_images", AsyncMock()))
        self.local = self.enterContext(
            patch(
                "playwright.async_api.async_playwright",
                side_effect=AssertionError("Do not start a second browser"),
            )
        )

    async def new_context(self, **_kwargs):
        self.assertTrue(self.in_operation)
        context = MagicMock()
        context.new_page = AsyncMock(return_value=self.page)
        context.route = AsyncMock()
        context.route_web_socket = AsyncMock()
        context.close = AsyncMock()
        self.contexts.append(context)
        return context

    async def test_shared_browser_independent_contexts_and_gateway(self):
        for _ in range(2):
            snapshot = await self.engine.capture("https://example.com/", self.directory)
            self.assertEqual(len(snapshot.paths), 1)
        self.assertEqual(self.operations, 2)
        self.assertEqual(self.provider.await_count, 2)
        self.assertEqual(len(self.contexts), 2)
        self.assertIsNot(*self.contexts)
        for context in self.contexts:
            context.close.assert_awaited_once()
            context.route.assert_awaited_once()
            context.route_web_socket.assert_awaited_once()
        options = self.browser.new_context.call_args.kwargs
        self.assertEqual(
            options["proxy"], {"server": self.gateway.address, "bypass": "<-loopback>"}
        )
        self.assertEqual(options["service_workers"], "block")
        self.assertFalse(options["accept_downloads"])
        self.assertNotIn("storage_state", options)
        self.browser.close.assert_not_awaited()
        self.local.assert_not_called()
        self.assertFalse(self.in_operation)
        # Policies are registered on our context, not on pre-existing shared ones.
        guard = self.contexts[0].route.await_args.args[1]
        for url, method, resource_type, blocked in (
            ("https://example.com/", "GET", "document", False),
            ("file:///etc/passwd", "GET", "document", True),
            ("https://example.com/", "POST", "document", True),
            ("https://example.com/stream", "GET", "eventsource", True),
        ):
            route = SimpleNamespace(
                request=SimpleNamespace(url=url, method=method, resource_type=resource_type),
                abort=AsyncMock(),
                continue_=AsyncMock(),
            )
            await guard(route)
            self.assertEqual(route.abort.await_count, int(blocked))
            self.assertEqual(route.continue_.await_count, int(not blocked))

    async def test_failure_closes_context_but_not_shared_browser(self):
        self.page.screenshot.side_effect = RuntimeError("secret-signed-url")
        with self.assertRaises(CaptureError) as caught:
            await self.engine.capture("https://example.com/", self.directory)
        self.assertNotIn("secret", str(caught.exception))
        self.contexts[0].close.assert_awaited_once()
        self.browser.close.assert_not_awaited()
        self.assertFalse(self.in_operation)
        self.local.assert_not_called()

    async def test_context_proxy_failure_does_not_retry_without_proxy(self):
        self.browser.new_context.side_effect = RuntimeError("proxy unsupported")
        with self.assertRaisesRegex(CaptureError, "内置浏览器"):
            await self.engine.capture("https://example.com/", self.directory)
        self.browser.new_context.assert_awaited_once()
        self.local.assert_not_called()
        self.browser.close.assert_not_awaited()
        self.assertFalse(self.in_operation)

    async def test_service_failure_does_not_spawn_another_browser(self):
        @asynccontextmanager
        async def fail():
            raise RuntimeError("service closed secret-token")
            yield  # pragma: no cover

        self.service.operation = fail
        with self.assertRaisesRegex(CaptureError, "内置浏览器") as caught:
            await self.engine.capture("https://example.com/", self.directory)
        self.assertNotIn("secret", str(caught.exception))
        self.browser.new_context.assert_not_awaited()
        self.local.assert_not_called()

    async def test_browser_preparation_status_reaches_chat_without_fallback(self):
        @asynccontextmanager
        async def preparing():
            raise BrowserUnavailable("正在后台下载 Chromium；请稍后再试。")
            yield  # pragma: no cover

        self.service.operation = preparing
        with self.assertRaisesRegex(CaptureError, "正在后台下载"):
            await self.engine.capture("https://example.com/", self.directory)
        self.local.assert_not_called()
        self.browser.new_context.assert_not_awaited()

    async def test_cancel_closes_only_our_context_and_releases_operation(self):
        started = asyncio.Event()

        async def block(**_kwargs):
            started.set()
            await asyncio.Future()

        self.page.screenshot.side_effect = block
        task = asyncio.create_task(self.engine.capture("https://example.com/", self.directory))
        await asyncio.wait_for(started.wait(), timeout=1)
        await self.engine.close()
        self.assertTrue(task.cancelled())
        self.contexts[0].close.assert_awaited_once()
        self.browser.close.assert_not_awaited()
        self.assertFalse(self.in_operation)
        self.assertFalse(self.engine._active)


if __name__ == "__main__":
    unittest.main()
