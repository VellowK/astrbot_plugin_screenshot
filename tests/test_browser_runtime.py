"""Owned browser bootstrap is offline/mocked; unit tests never install browsers."""

import asyncio
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_screenshot.browser import (
    CLOSED_PROXY,
    BrowserRuntime,
    BrowserUnavailable,
    launch_options,
)
from astrbot_plugin_screenshot.config import ConfigError, Settings


class BrowserRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = Settings(browser_idle_seconds=0, browser_install_retries=0)
        self.config = {}
        self.logger = MagicMock()
        self.runtime = BrowserRuntime(self.settings, self.config, logger=self.logger)
        self.addAsyncCleanup(self.runtime.close)
        self.browser = MagicMock()
        self.browser.is_connected.return_value = True
        self.browser.close = AsyncMock()
        self.context = MagicMock()
        self.context.close = AsyncMock()
        self.context.route = AsyncMock()
        self.page = MagicMock(set_content=AsyncMock(), screenshot=AsyncMock())
        self.context.new_page = AsyncMock(return_value=self.page)
        self.browser.new_context = AsyncMock(return_value=self.context)
        self.pw = SimpleNamespace(
            chromium=SimpleNamespace(launch=AsyncMock(return_value=self.browser)), stop=AsyncMock()
        )
        manager = MagicMock(start=AsyncMock(return_value=self.pw))
        self.factory = self.enterContext(
            patch("playwright.async_api.async_playwright", return_value=manager)
        )
        self.install = self.enterContext(patch.object(self.runtime, "_install_once", AsyncMock()))

    async def prepare(self):
        await self.runtime.start()
        if self.runtime._setup_task is not None:
            await asyncio.wait_for(self.runtime._setup_task, timeout=2)

    async def test_existing_browser_probe_and_automatic_status(self):
        self.config.update(browser_ready=True, browser_status="forged")
        await self.runtime.start()
        self.assertFalse(self.config["browser_ready"])
        await self.runtime._setup_task
        self.assertTrue(self.runtime.ready)
        self.assertTrue(self.config["browser_ready"])
        self.assertIn("已就绪", self.config["browser_status"])
        self.context.close.assert_awaited_once()
        self.page.screenshot.assert_awaited_once()
        self.assertEqual(self.browser.new_context.call_args.kwargs["proxy"]["server"], CLOSED_PROXY)
        self.assertTrue(self.pw.chromium.launch.call_args.kwargs["chromium_sandbox"])
        self.install.assert_not_awaited()
        for _ in range(2):
            async with self.runtime.operation() as browser:
                self.assertIs(browser, self.browser)
                self.assertEqual(self.runtime._users, 1)
        self.assertEqual(self.runtime._users, 0)
        self.pw.chromium.launch.assert_awaited_once()
        await self.runtime.start()
        self.pw.chromium.launch.assert_awaited_once()

    async def test_missing_browser_installs_once_then_probes(self):
        self.pw.chromium.launch.side_effect = [
            RuntimeError("Executable doesn't exist at secret-path"),
            self.browser,
        ]
        await self.prepare()
        self.install.assert_awaited_once()
        self.assertTrue(self.runtime.ready)
        self.assertEqual(self.pw.chromium.launch.await_count, 2)

    async def test_no_download_when_disabled_or_custom_path(self):
        for changes in (
            {"auto_install_browser": False},
            {"executable_path": "/not-installed/chrome"},
        ):
            with self.subTest(changes=changes):
                self.runtime.settings = replace(self.settings, **changes)
                self.pw.chromium.launch.side_effect = RuntimeError(
                    "Executable doesn't exist secret"
                )
                await self.runtime._prepare()
                self.install.assert_not_awaited()
                self.assertFalse(self.runtime.ready)
                self.assertNotIn("secret", self.config["browser_status"])

    async def test_plugin_disabled_does_not_start_driver(self):
        self.runtime.settings = replace(self.settings, enabled=False)
        await self.prepare()
        self.factory.assert_not_called()
        self.install.assert_not_awaited()
        self.assertFalse(self.config["browser_ready"])
        self.assertIn("禁用", self.config["browser_status"])

    async def test_system_library_and_sandbox_errors_never_download_or_disable_sandbox(self):
        for error, hint in (
            ("Host system is missing dependencies to run browsers secret", "系统库"),
            ("Running as root without --no-sandbox is not supported secret", "沙箱"),
            ("EPERM: operation not permitted secret", "权限"),
            ("unexpected browser failure secret", "启动或探测失败"),
        ):
            with self.subTest(hint=hint):
                self.pw.chromium.launch.side_effect = RuntimeError(error)
                await self.runtime._prepare()
                self.assertFalse(self.runtime.ready)
                self.assertIn(hint, self.config["browser_status"])
                self.assertNotIn("secret", self.config["browser_status"])
                self.assertTrue(self.pw.chromium.launch.call_args.kwargs["chromium_sandbox"])
                self.install.assert_not_awaited()

    async def test_probe_failure_closes_browser_and_never_marks_ready(self):
        self.browser.new_context.side_effect = RuntimeError("proxy unsupported secret")
        await self.prepare()
        self.assertFalse(self.runtime.ready)
        self.browser.close.assert_awaited_once()
        self.pw.stop.assert_awaited_once()
        self.install.assert_not_awaited()

    async def test_download_retry_is_bounded_and_status_safe(self):
        self.runtime.settings = replace(self.settings, browser_install_retries=1)
        self.pw.chromium.launch.side_effect = RuntimeError("Executable doesn't exist")
        self.install.side_effect = BrowserUnavailable("Chromium 下载超时；请检查下载代理。")
        with patch("astrbot_plugin_screenshot.browser.asyncio.sleep", AsyncMock()) as sleep:
            await self.prepare()
        self.assertEqual(self.install.await_count, 2)
        sleep.assert_awaited_once_with(30)
        self.assertFalse(self.runtime.ready)
        for _ in range(2):
            with self.assertRaisesRegex(BrowserUnavailable, "下载超时"):
                async with self.runtime.operation():
                    self.fail("Must not be yielded")
        self.assertEqual(self.install.await_count, 2)

    async def test_not_ready_and_closed_never_start_a_browser(self):
        with self.assertRaises(BrowserUnavailable):
            async with self.runtime.operation():
                pass
        await self.runtime.close()
        with self.assertRaisesRegex(BrowserUnavailable, "关闭"):
            async with self.runtime.operation():
                pass
        self.factory.assert_not_called()
        self.assertIs(await self.runtime.get_service(), self.runtime)

    async def test_idle_sleep_and_wake_reuses_driver(self):
        await self.prepare()
        self.runtime.settings = replace(self.settings, browser_idle_seconds=1)
        self.runtime._last_used = 0
        async with self.runtime.operation():
            await self.runtime._expire_idle()
            self.browser.close.assert_not_awaited()
        self.runtime._last_used = 0
        await self.runtime._expire_idle()
        self.assertIsNone(self.runtime._browser)
        self.assertTrue(self.runtime.ready)
        self.assertIn("休眠", self.config["browser_status"])
        async with self.runtime.operation():
            self.assertIs(self.runtime._browser, self.browser)
        self.assertEqual(self.pw.chromium.launch.await_count, 2)
        self.factory.assert_called_once()
        self.pw.stop.assert_not_awaited()

    async def test_browser_crash_wakes_without_downloading(self):
        await self.prepare()
        self.browser.is_connected.return_value = False
        async with self.runtime.operation():
            self.assertEqual(self.runtime._users, 1)
        self.assertEqual(self.pw.chromium.launch.await_count, 2)
        self.install.assert_not_awaited()

    async def test_unload_cancels_preparation_and_owned_operations(self):
        started = asyncio.Event()

        async def block():
            started.set()
            await asyncio.Future()

        self.pw.chromium.launch.side_effect = RuntimeError("Executable doesn't exist")
        self.install.side_effect = block
        await self.runtime.start()
        await asyncio.wait_for(started.wait(), timeout=1)
        await self.runtime.close()
        self.assertTrue(self.runtime._setup_task.cancelled())
        self.pw.stop.assert_awaited_once()
        self.assertFalse(self.config["browser_ready"])

    async def test_close_cancels_inflight_operation_and_releases_count(self):
        await self.prepare()
        started = asyncio.Event()

        async def capture():
            async with self.runtime.operation():
                started.set()
                await asyncio.Future()

        task = asyncio.create_task(capture())
        await asyncio.wait_for(started.wait(), timeout=1)
        await self.runtime.close()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.runtime._users, 0)
        self.assertFalse(self.runtime._operations)
        self.browser.close.assert_awaited_once()

    async def test_status_save_failure_does_not_prevent_ready(self):
        class Config(dict):
            save_config_async = AsyncMock(side_effect=OSError("secret-proxy-password"))

        self.runtime.config = config = Config()
        await self.prepare()
        self.assertTrue(self.runtime.ready)
        self.assertTrue(config["browser_ready"])
        self.assertGreaterEqual(config.save_config_async.await_count, 2)
        self.assertNotIn("secret-proxy", str(self.logger.mock_calls))

    async def test_sync_and_async_config_savers(self):
        class Config(dict):
            pass

        for saver in (MagicMock(), AsyncMock()):
            config = Config()
            config.save_config = saver
            self.runtime.config = config
            await self.runtime._status(False, "testing")
            saver.assert_called_once()
            self.assertFalse(self.runtime._save_pending)


class InstallerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = BrowserRuntime(Settings(), {}, logger=MagicMock())

    async def test_official_cli_same_python_and_scoped_download_proxy(self):
        self.runtime.settings = Settings(browser_install_proxy="http://user:secret@localhost:7890")
        proc = SimpleNamespace(wait=AsyncMock(return_value=0), returncode=0)
        with (
            patch(
                "astrbot_plugin_screenshot.browser.asyncio.create_subprocess_exec",
                AsyncMock(return_value=proc),
            ) as spawn,
            patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "/browser-cache"}),
        ):
            await self.runtime._install_once()
        args, options = spawn.call_args
        self.assertEqual(args, (sys.executable, "-m", "playwright", "install", "chromium"))
        self.assertEqual(options["env"]["HTTPS_PROXY"], self.runtime.settings.browser_install_proxy)
        self.assertEqual(options["env"]["PLAYWRIGHT_BROWSERS_PATH"], "/browser-cache")
        self.assertNotIn("shell", options)
        self.assertIsNone(self.runtime._process)
        self.assertNotEqual(
            os.environ.get("HTTPS_PROXY"), self.runtime.settings.browser_install_proxy
        )

    async def test_failed_and_timed_out_cli_always_cleans_up(self):
        for outcome, hint in ((1, "下载失败"), (TimeoutError(), "下载超时")):
            proc = SimpleNamespace(returncode=0, wait=AsyncMock())
            if isinstance(outcome, Exception):
                proc.wait.side_effect = outcome
            else:
                proc.wait.return_value = outcome
            with (
                patch(
                    "astrbot_plugin_screenshot.browser.asyncio.create_subprocess_exec",
                    AsyncMock(return_value=proc),
                ),
                patch.object(self.runtime, "_stop_install", AsyncMock()) as cleanup,
            ):
                with self.assertRaisesRegex(BrowserUnavailable, hint):
                    await self.runtime._install_once()
                cleanup.assert_awaited_once()

    async def test_posix_installer_tree_cleanup(self):
        proc = SimpleNamespace(pid=43210, returncode=None, kill=MagicMock(), wait=AsyncMock())
        self.runtime._process = proc
        with (
            patch("astrbot_plugin_screenshot.browser.sys.platform", "linux"),
            patch("astrbot_plugin_screenshot.browser.os.killpg", create=True) as killpg,
            patch("astrbot_plugin_screenshot.browser.signal.SIGKILL", 9, create=True),
        ):
            await self.runtime._stop_install()
        self.assertEqual(killpg.call_args.args[0], proc.pid)
        proc.wait.assert_awaited_once()
        self.assertIsNone(self.runtime._process)

    async def test_windows_installer_tree_cleanup(self):
        proc = SimpleNamespace(pid=43210, returncode=None, kill=MagicMock(), wait=AsyncMock())
        self.runtime._process = proc
        killer = SimpleNamespace(wait=AsyncMock(return_value=0))
        with (
            patch("astrbot_plugin_screenshot.browser.sys.platform", "win32"),
            patch(
                "astrbot_plugin_screenshot.browser.asyncio.create_subprocess_exec",
                AsyncMock(return_value=killer),
            ) as spawn,
        ):
            await self.runtime._stop_install()
        self.assertEqual(spawn.call_args.args[1:], ("/PID", "43210", "/T", "/F"))
        proc.wait.assert_awaited_once()
        self.assertIsNone(self.runtime._process)


class BrowserConfigTests(unittest.TestCase):
    def test_defaults_and_legacy_status_ignored(self):
        self.assertEqual(
            Settings.load({"browser_ready": True, "browser_status": "forged"}), Settings()
        )
        self.assertEqual(
            Settings.load({"auto_detect_browser": True, "browser_plugin_detected": True}),
            Settings(),
        )
        self.assertTrue(Settings().auto_install_browser)
        for config in (
            {"auto_install_browser": 1},
            {"browser_install_timeout": 0},
            {"browser_install_retries": 4},
            {"browser_idle_seconds": -1},
            {"browser_install_proxy": "socks5://localhost:1080"},
            {"browser_install_proxy": "http://localhost/invalid"},
            {"browser_install_proxy": 123},
        ):
            with self.subTest(config=config), self.assertRaises(ConfigError):
                Settings.load(config)

    def test_launch_sandbox_default_and_override(self):
        self.assertTrue(launch_options(Settings(), CLOSED_PROXY)["chromium_sandbox"])
        options = launch_options(
            Settings(no_sandbox=True, executable_path="/opt/chrome"), CLOSED_PROXY
        )
        self.assertFalse(options["chromium_sandbox"])
        self.assertIn("--no-sandbox", options["args"])
        self.assertEqual(options["executable_path"], "/opt/chrome")


if __name__ == "__main__":
    unittest.main()
