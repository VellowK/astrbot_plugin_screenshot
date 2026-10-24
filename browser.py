"""Owned Chromium lifecycle and optional official CLI download; no plugin dependency."""

import asyncio
import inspect
import os
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from .config import Settings

# The process has no direct default egress. Every capture overrides this with its
# own validating gateway at context level, including on Windows Chromium.
CLOSED_PROXY = "http://127.0.0.1:9"


class BrowserUnavailable(RuntimeError):
    """Only fixed, credential-free diagnostics may be exposed to users."""


def launch_options(settings: Settings, proxy: str) -> dict:
    args = [
        "--proxy-bypass-list=<-loopback>",
        "--disable-quic",
        "--disable-background-networking",
        "--disable-component-update",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-features=DnsOverHttps,WebTransport",
    ]
    if settings.no_sandbox:
        args.append("--no-sandbox")
    options = dict(
        headless=True,
        args=args,
        proxy={"server": proxy},
        chromium_sandbox=not settings.no_sandbox,
        timeout=15000,
    )
    if settings.executable_path:
        options["executable_path"] = settings.executable_path
    return options


class BrowserRuntime:
    def __init__(self, settings: Settings, config, *, logger):
        self.settings = settings
        self.config = config
        self.logger = logger
        self.ready = False
        self.status = "浏览器尚未初始化；请重载插件。"
        self._lock = asyncio.Lock()
        self._setup_task = None
        self._monitor = None
        self._pw = None
        self._browser = None
        self._process = None
        self._closed = False
        self._started = False
        self._users = 0
        self._operations = set()
        self._last_used = time.monotonic()
        self._save_pending = False

    async def _status(self, ready: bool, text: str):
        self.ready, self.status = ready, text
        if (
            self.config.get("browser_ready") is not ready
            or self.config.get("browser_status") != text
        ):
            self.config["browser_ready"] = ready
            self.config["browser_status"] = text
            self._save_pending = True
            self.logger.info("Screenshot browser: %s", text)
        if self._save_pending:
            saver = getattr(self.config, "save_config_async", None)
            if not callable(saver):
                saver = getattr(self.config, "save_config", None)
            if callable(saver):
                try:
                    result = saver()
                    if inspect.isawaitable(result):
                        await result
                    self._save_pending = False
                except Exception as exc:
                    self.logger.warning("Browser status save failed (%s).", type(exc).__name__)
            else:
                self._save_pending = False

    async def start(self):
        if self._closed or self._started:
            return
        self._started = True
        if not self.settings.enabled:
            await self._status(False, "截图插件已禁用，未启动或下载浏览器。")
            return
        await self._status(False, "正在后台检查浏览器；准备期间请稍后再试。")
        self._setup_task = asyncio.create_task(self._prepare())

    async def get_service(self):
        # Always return this runtime: never fall back to an unmanaged second browser.
        return self

    async def _prepare(self):
        try:
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise BrowserUnavailable(
                    "缺少 Playwright，请通过插件管理安装 Python 依赖后重载。"
                ) from exc
            self._pw = await asyncio.wait_for(async_playwright().start(), timeout=30)
            try:
                await self._launch()
            except Exception as exc:
                missing = "Executable doesn't exist" in str(exc)
                if self.settings.executable_path or not missing:
                    raise
                if not self.settings.auto_install_browser:
                    raise BrowserUnavailable(
                        "缺少 Chromium；开启 auto_install_browser 并重载，或手动安装浏览器。"
                    ) from exc
                await self._download()
                await self._launch()
            await self._status(True, "内置 Chromium 已就绪。")
            self._last_used = time.monotonic()
            if self.settings.browser_idle_seconds:
                self._monitor = asyncio.create_task(self._watch_idle())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning("Browser preparation failed (%s).", type(exc).__name__)
            await self._status(False, self._diagnostic(exc))
            await self._dispose()

    def _diagnostic(self, exc: Exception) -> str:
        if isinstance(exc, BrowserUnavailable):
            return str(exc)
        text = str(exc).lower()  # Classify only; never log or persist raw exceptions.
        if self.settings.executable_path and (
            "executable" in text or "enoent" in text or "not found" in text
        ):
            return "自定义浏览器不可用；请检查 executable_path、权限和版本兼容性后重载。"
        if any(
            token in text
            for token in ("missing dependencies", "error while loading shared libraries", "libnss3")
        ):
            return "Chromium 缺少 Linux 系统库；请管理员在同一容器安装系统依赖后重载，详见 README。"
        if any(token in text for token in ("sandbox", "running as root")):
            return "Chromium 沙箱不可用；优先改用非 root 和可用沙箱。仅隔离容器可按需开启 no_sandbox 后重载。"
        if (
            isinstance(exc, (PermissionError, OSError))
            or "eperm" in text
            or "permission denied" in text
        ):
            return "浏览器启动被拒绝；请检查缓存、临时目录及执行权限后重载。"
        if isinstance(exc, TimeoutError):
            return "浏览器启动超时；请检查资源与容器环境后重载。"
        return "浏览器启动或探测失败；请检查系统库、权限、沙箱和版本兼容性后重载，详见 README。"

    async def _launch(self):
        browser = await self._pw.chromium.launch(**launch_options(self.settings, CLOSED_PROXY))
        try:
            # Offline probe includes independent context proxy support and rendering.
            # Do not mark ready simply because an executable exists on disk.
            async with asyncio.timeout(15):
                context = await browser.new_context(
                    viewport={"width": 64, "height": 64},
                    proxy={"server": CLOSED_PROXY, "bypass": "<-loopback>"},
                    java_script_enabled=False,
                    service_workers="block",
                    accept_downloads=False,
                )
                try:
                    await context.route("**/*", lambda route: route.abort())
                    page = await context.new_page()
                    await page.set_content("<!doctype html><title>ready</title><p>ready</p>")
                    await page.screenshot(timeout=5000)
                finally:
                    await asyncio.wait_for(context.close(), timeout=5)
        except BaseException:
            await self._close_browser(browser)
            raise
        self._browser = browser

    async def _download(self):
        attempts = self.settings.browser_install_retries + 1
        for attempt in range(1, attempts + 1):
            await self._status(
                False, f"正在后台下载 Chromium（{attempt}/{attempts}）；请稍后刷新面板。"
            )
            try:
                await self._install_once()
                await self._status(False, "Chromium 下载完成，正在启动并验证渲染。")
                return
            except BrowserUnavailable as exc:
                if attempt == attempts:
                    raise
                await self._status(False, str(exc) + " 30 秒后自动重试。")
                await asyncio.sleep(30)

    async def _install_once(self):
        env = os.environ.copy()
        if self.settings.browser_install_proxy:
            env["HTTPS_PROXY"] = self.settings.browser_install_proxy
            env["HTTP_PROXY"] = self.settings.browser_install_proxy
        # Keep PLAYWRIGHT_BROWSERS_PATH and official mirror/CA environment settings.
        # No shell, pip, sudo, apt, arbitrary URL or browser-dependency downgrade.
        group = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        try:
            self._process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "playwright",
                "install",
                "chromium",
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **group,
            )
            code = await asyncio.wait_for(
                self._process.wait(), timeout=self.settings.browser_install_timeout
            )
            if code:
                raise BrowserUnavailable(
                    f"Chromium 下载失败（退出码 {code}）；请检查下载代理、网络及缓存写权限后重载。"
                )
        except TimeoutError as exc:
            raise BrowserUnavailable(
                "Chromium 下载超时；请检查下载代理或增大安装超时后重载。"
            ) from exc
        except OSError as exc:
            raise BrowserUnavailable(
                "无法启动浏览器安装器；请检查 Python 环境与执行权限后重载。"
            ) from exc
        finally:
            await self._stop_install()

    async def _stop_install(self):
        proc, self._process = self._process, None
        if proc is None or proc.returncode is not None:
            return
        # Playwright's Python CLI starts a Node child. Kill its process tree, not
        # only the Python parent, on timeout/unload so downloads do not survive us.
        try:
            if sys.platform == "win32":
                killer = (
                    Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
                )
                child = await asyncio.create_subprocess_exec(
                    str(killer),
                    "/PID",
                    str(proc.pid),
                    "/T",
                    "/F",
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                await asyncio.wait_for(child.wait(), timeout=10)
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, TimeoutError) as exc:
            self.logger.warning("Browser installer tree cleanup failed (%s).", type(exc).__name__)
        try:
            if proc.returncode is None:
                proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (OSError, TimeoutError) as exc:
            self.logger.warning("Browser installer cleanup failed (%s).", type(exc).__name__)

    @asynccontextmanager
    async def operation(self):
        task = asyncio.current_task()
        async with self._lock:
            if self._closed:
                raise BrowserUnavailable("浏览器正在关闭，请稍后重试。")
            if not self.ready:
                raise BrowserUnavailable(self.status)
            if self._browser is None or not self._browser.is_connected():
                await self._close_browser(self._browser)
                self._browser = None
                try:
                    await self._launch()
                    await self._status(True, "内置 Chromium 已就绪。")
                except Exception as exc:
                    text = self._diagnostic(exc)
                    await self._status(False, text)
                    raise BrowserUnavailable(text) from exc
            browser = self._browser
            self._users += 1
            self._operations.add(task)
        try:
            yield browser
        finally:
            async with self._lock:
                self._users -= 1
                self._operations.discard(task)
                self._last_used = time.monotonic()

    async def _watch_idle(self):
        interval = min(15, self.settings.browser_idle_seconds)
        while True:
            await asyncio.sleep(interval)
            await self._expire_idle()

    async def _expire_idle(self):
        async with self._lock:
            if (
                not self._closed
                and not self._users
                and self._browser is not None
                and time.monotonic() - self._last_used >= self.settings.browser_idle_seconds
            ):
                await self._close_browser(self._browser)
                self._browser = None
                await self._status(True, "内置 Chromium 已就绪（空闲休眠，下次截图自动唤醒）。")

    async def _close_browser(self, browser):
        if browser is not None:
            try:
                await asyncio.wait_for(browser.close(), timeout=5)
            except Exception as exc:
                self.logger.warning("Browser close failed (%s).", type(exc).__name__)

    async def _dispose(self):
        await self._close_browser(self._browser)
        self._browser = None
        if self._pw is not None:
            try:
                await asyncio.wait_for(self._pw.stop(), timeout=5)
            except Exception as exc:
                self.logger.warning("Playwright stop failed (%s).", type(exc).__name__)
            self._pw = None

    async def close(self):
        if self._closed:
            return
        self._closed = True
        tasks = [t for t in (self._setup_task, self._monitor, *self._operations) if t is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._stop_install()
        await self._dispose()
        await self._status(False, "浏览器已停止；重载插件后重新检查。")
