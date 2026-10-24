"""AstrBot entry point. The fallback never preempts another parser's response."""

from pathlib import Path
from tempfile import TemporaryDirectory

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .browser import BrowserRuntime
from .config import Settings
from .cooperation import observe_responses, remember_response, response_seen, send_screenshot
from .engine import CaptureError, ScreenshotEngine
from .fallback import Reservations, candidates, event_handled, public_label

HELP = """网页截图（内置 Chromium 管理；不需要浏览器前置插件或截图 API）
• 直接发送 HTTP(S) 链接：其他解析器未处理时自动兜底。
• /截图 https://example.com：手动截图，可覆盖自动跳过域名，但不能绕过访问策略。
• /截图 帮助：显示帮助。
NodeSeek：正文 + 当前第一页可见的置顶 / Hot 评论，不抓取全部评论。
遇到登录、Cloudflare 验证或站点拒绝时不会自动绕过。
其他插件已回复则自动避让；独立客户端 / 延迟发送等场景可用 skip_domains 排除。"""


class ScreenshotPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.settings = Settings.load(config)
        self.browser_runtime = BrowserRuntime(self.settings, config, logger=logger)
        self.engine = ScreenshotEngine(
            self.settings, browser_provider=self.browser_runtime.get_service
        )
        self.reservations = Reservations(self.settings)
        self.root = (
            Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_screenshot" / "cache"
        )
        self.root.mkdir(parents=True, exist_ok=True)

    async def initialize(self):
        await self.browser_runtime.start()

    def _session_allowed(self, event) -> bool:
        return (
            not self.settings.allowed_sessions
            or event.unified_msg_origin in self.settings.allowed_sessions
        )

    @staticmethod
    def _is_self_message(event: AstrMessageEvent) -> bool:
        sender_id = event.get_sender_id()
        self_id = event.get_self_id()
        # Adapters can expose numeric IDs as either integers or strings.
        # Missing IDs must not cause unrelated messages to be discarded.
        if sender_id is None or self_id is None:
            return False
        sender_id, self_id = str(sender_id).strip(), str(self_id).strip()
        return bool(sender_id and self_id and sender_id == self_id)

    @filter.command("截图")
    async def screenshot(self, event: AstrMessageEvent):
        """手动截取指定网页，或显示帮助。"""
        if self._is_self_message(event):
            return
        event.set_extra("screenshot_command", True)
        if not self._session_allowed(event):
            return
        if not self.settings.enabled:
            await send_screenshot(event, event.plain_result("网页截图插件已禁用。"))
            return
        urls = candidates(event, self.settings, explicit=True)
        if not urls:
            await send_screenshot(event, event.plain_result(HELP))
            event.stop_event()
            return
        await self._run(event, urls, explicit=True)
        event.stop_event()

    def _auto_allowed(self, event) -> bool:
        return (
            not self._is_self_message(event)
            and self.settings.enabled
            and self.settings.auto_enabled
            and self._session_allowed(event)
            and not event.get_extra("screenshot_command", False)
            and not event.message_str.lstrip().startswith("/")
            and (not self.settings.require_at or event.is_at_or_wake_command)
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def observe(self, event: AstrMessageEvent):
        # Observation only: no capture, stop_event, result changes or shared-client patch.
        if self._auto_allowed(event) and candidates(event, self.settings):
            observe_responses(event)

    @filter.after_message_sent(priority=10000)
    async def remember_sent(self, event: AstrMessageEvent):
        # Pipeline sends can be followed by clear_result and a parser restoring its
        # original _has_send_oper. The independent event flag survives both.
        result = event.get_result()
        if result and getattr(result, "chain", None):
            remember_response(event)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-10000)
    async def fallback(self, event: AstrMessageEvent):
        if not self._auto_allowed(event):
            return
        urls = candidates(event, self.settings)
        if urls:
            await self._run(event, urls, explicit=False)

    @staticmethod
    def _defer(event, sent_any: bool) -> bool:
        return response_seen(event) or event.is_stopped() or (not sent_any and event_handled(event))

    async def _run(self, event, urls, *, explicit):
        sent_any = False
        admitted = False
        for url in urls:
            if not explicit and self._defer(event, sent_any):
                break
            # Earlier messages in this same event must not make us suppress our own second URL.
            key = self.reservations.reserve(
                event.unified_msg_origin,
                event.get_sender_id(),
                url,
                same_batch=admitted,
            )
            if key is None:
                if explicit and not sent_any:
                    await send_screenshot(
                        event,
                        event.plain_result(
                            "请求过于频繁、已有相同截图或截图队列已满，请稍后再试。"
                        ),
                    )
                continue
            admitted = True
            success = False
            try:
                with TemporaryDirectory(prefix="capture-", dir=self.root) as temp:
                    snapshot = await self.engine.capture(url, Path(temp))
                    for i, path in enumerate(snapshot.paths):
                        # A parser's background task may finish while Chromium is busy.
                        if not explicit and self._defer(event, sent_any):
                            break
                        chain = [Image.fromFileSystem(str(path))]
                        if i == 0:
                            chain.insert(0, Plain(f"{public_label(url)} · {snapshot.note}\n"))
                        await send_screenshot(event, event.chain_result(chain))
                        sent_any = True
                    else:
                        success = bool(snapshot.paths)
                    if success:
                        handled = event.get_extra("screenshot_handled_urls", [])
                        handled = list(handled) if isinstance(handled, (list, tuple, set)) else []
                        event.set_extra("screenshot_handled_urls", handled + [url])
            except CaptureError as e:
                logger.warning("Screenshot capture failed (%s).", type(e).__name__)
                if explicit or (
                    self.settings.report_auto_errors and not self._defer(event, sent_any)
                ):
                    await send_screenshot(event, event.plain_result(str(e)))
            except Exception as e:
                # Raw exception text can contain proxy credentials or signed URLs.
                logger.warning("Screenshot delivery failed (%s).", type(e).__name__)
                if explicit:
                    await send_screenshot(
                        event,
                        event.plain_result(
                            "截图或图片发送失败，请检查日志中的错误类型和平台状态。"
                        ),
                    )
            finally:
                self.reservations.finish(key, success)
        if sent_any:
            event.set_extra("screenshot_handled", True)
            event.stop_event()

    async def terminate(self):
        # Cancel captures first so their contexts finish before the browser closes.
        await self.engine.close()
        await self.browser_runtime.close()
