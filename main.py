"""AstrBot entry point. The fallback never preempts another parser's response."""

from pathlib import Path
from tempfile import TemporaryDirectory

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .config import Settings
from .engine import CaptureError, ScreenshotEngine
from .fallback import Reservations, candidates, public_label

HELP = """网页截图（本机 Chromium，不使用截图 API）
• 直接发送 HTTP(S) 链接：其他解析器未处理时自动兜底。
• /截图 https://example.com：手动截图，可覆盖自动跳过域名，但不能绕过访问策略。
• /截图 帮助：显示帮助。
NodeSeek：正文 + 当前第一页可见的置顶 / Hot 评论，不抓取全部评论。
遇到登录、Cloudflare 验证或站点拒绝时不会自动绕过。
自动解析建议配置 skip_domains，排除已有专用解析器的域名。"""


class ScreenshotPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.settings = Settings.load(config)
        self.engine = ScreenshotEngine(self.settings)
        self.reservations = Reservations(self.settings)
        self.root = (
            Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_screenshot" / "cache"
        )
        self.root.mkdir(parents=True, exist_ok=True)

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
            await event.send(event.plain_result("网页截图插件已禁用。"))
            return
        urls = candidates(event, self.settings, explicit=True)
        if not urls:
            await event.send(event.plain_result(HELP))
            event.stop_event()
            return
        await self._run(event, urls, explicit=True)
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-10000)
    async def fallback(self, event: AstrMessageEvent):
        settings = self.settings
        if (
            self._is_self_message(event)
            or not settings.enabled
            or not settings.auto_enabled
            or not self._session_allowed(event)
            or event.get_extra("screenshot_command", False)
            or event.message_str.lstrip().startswith("/")
            or (settings.require_at and not event.is_at_or_wake_command)
        ):
            return
        urls = candidates(event, settings)
        if urls:
            await self._run(event, urls, explicit=False)

    async def _run(self, event, urls, *, explicit):
        sent_any = False
        admitted = False
        for url in urls:
            # Earlier messages in this same event must not make us suppress our own second URL.
            key = self.reservations.reserve(
                event.unified_msg_origin,
                event.get_sender_id(),
                url,
                same_batch=admitted,
            )
            if key is None:
                if explicit and not sent_any:
                    await event.send(
                        event.plain_result("请求过于频繁、已有相同截图或截图队列已满，请稍后再试。")
                    )
                continue
            admitted = True
            success = False
            try:
                with TemporaryDirectory(prefix="capture-", dir=self.root) as temp:
                    snapshot = await self.engine.capture(url, Path(temp))
                    for i, path in enumerate(snapshot.paths):
                        chain = [Image.fromFileSystem(str(path))]
                        if i == 0:
                            chain.insert(0, Plain(f"{public_label(url)} · {snapshot.note}\n"))
                        await event.send(event.chain_result(chain))
                        sent_any = True
                    success = True
                    handled = event.get_extra("screenshot_handled_urls", [])
                    handled = list(handled) if isinstance(handled, (list, tuple, set)) else []
                    event.set_extra("screenshot_handled_urls", handled + [url])
            except CaptureError as e:
                logger.warning("Screenshot capture failed (%s).", type(e).__name__)
                if explicit or self.settings.report_auto_errors:
                    await event.send(event.plain_result(str(e)))
            except Exception as e:
                # Raw exception text can contain proxy credentials or signed URLs.
                logger.warning("Screenshot delivery failed (%s).", type(e).__name__)
                if explicit:
                    await event.send(
                        event.plain_result("截图或图片发送失败，请检查日志中的错误类型和平台状态。")
                    )
            finally:
                self.reservations.finish(key, success)
        if sent_any:
            event.set_extra("screenshot_handled", True)
            event.stop_event()

    async def terminate(self):
        await self.engine.close()
