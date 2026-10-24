"""Local, isolated Chromium snapshots. No remote screenshot service or browser profile."""

import asyncio
import math
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from .config import Settings
from .gateway import Gateway
from .policy import UnsafeURL, capture_url, nodeseek_url, normalize_url, resolve_public, selectable


class CaptureError(RuntimeError):
    """Safe to show to a chat user; never contains raw browser/proxy errors."""


@dataclass
class Snapshot:
    paths: list[Path]
    note: str


CARD_CSS = """
*{box-sizing:border-box}html{background:#f0f3f7;color:#172033;font:18px/1.75 Arial,'Microsoft YaHei','Noto Sans CJK SC',sans-serif}
body{margin:0;padding:32px}main{max-width:1050px;margin:auto}.card{background:white;border:1px solid #dce3ed;border-radius:16px;padding:28px;margin-bottom:22px;overflow:hidden}
h1{font-size:30px;line-height:1.45;margin:0 0 16px}.meta,.source,.notice{font-size:14px;color:#637087;overflow-wrap:anywhere}.source{margin-bottom:20px}.meta{margin-bottom:20px}
article{overflow-wrap:anywhere}article img{display:block;max-width:100%;height:auto;margin:12px auto;max-height:2000px;object-fit:contain}
pre{white-space:pre-wrap;background:#f5f7fa;padding:16px;border-radius:8px;font-size:15px}code{font-family:Consolas,monospace}blockquote{margin:12px 0;padding:8px 18px;border-left:4px solid #b6c9e0;background:#f7f9fc}
table{border-collapse:collapse;max-width:100%;font-size:15px}td,th{border:1px solid #dce3ed;padding:7px}a{color:#2866b5}h2{font-size:22px}.badge{display:inline-block;font-size:13px;color:#a94716;background:#fff1e4;border-radius:5px;padding:1px 8px;margin-right:8px}
"""


def nodeseek_html(data: dict, source: str) -> str:
    def meta(item):
        return escape(" · ".join(str(item[k]) for k in ("author", "floor", "time") if item.get(k)))

    parts = [
        f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>{CARD_CSS}</style><body><main>',
        '<div class="source">NodeSeek · 正文与精选评论快照<br>' + escape(source) + "</div>",
        '<section class="card"><h1>' + escape(data["title"]) + "</h1>",
        '<div class="meta">'
        + meta(data)
        + "</div><article>"
        + data["html"]
        + "</article></section>",
    ]
    if data["comments"]:
        parts.append("<h2>置顶 / Hot 评论（当前第一页）</h2>")
    for item in data["comments"]:
        badge = '<span class="badge">置顶</span>' if item["pinned"] else ""
        badge += '<span class="badge">HOT</span>' if item["hot"] else ""
        parts.append(
            '<section class="card">'
            + badge
            + '<div class="meta">'
            + meta(item)
            + "</div><article>"
            + item["html"]
            + "</article></section>"
        )
    if data["total"] > len(data["comments"]):
        parts.append('<p class="notice">精选评论数量超过上限，仅显示部分。</p>')
    if not data["total"]:
        parts.append(
            '<p class="notice">当前第一页未发现带置顶 / Hot 标记的评论；不抓取其他分页。</p>'
        )
    parts.append("</main></body></html>")
    return "".join(parts)


class ScreenshotEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.concurrency)
        self._active: set[asyncio.Task] = set()
        self._closed = False

    async def close(self):
        self._closed = True
        tasks = list(self._active)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def capture(self, url: str, directory: Path) -> Snapshot:
        if self._closed:
            raise CaptureError("截图插件正在关闭。")
        task = asyncio.current_task()
        self._active.add(task)
        try:
            url = capture_url(normalize_url(url))
            if not selectable(url, self.settings, explicit=True):
                raise CaptureError("该域名被访问策略禁止。")
            # Includes queueing, DNS, browser launch, navigation, layout and screenshot.
            async with asyncio.timeout(self.settings.timeout):
                async with self._semaphore:
                    return await self._capture(url, directory)
        except CaptureError:
            raise
        except UnsafeURL as e:
            raise CaptureError(str(e)) from e
        except TimeoutError as e:
            raise CaptureError("截图超时；请稍后再试或检查代理路由。") from e
        except Exception as e:
            raise CaptureError("截图失败；请检查 Chromium 安装、网络和代理配置。") from e
        finally:
            self._active.discard(task)

    async def _capture(self, url: str, directory: Path) -> Snapshot:
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise CaptureError("缺少 Playwright；请安装插件依赖和 Chromium，详见 README。") from e
        settings = self.settings
        parsed = urlsplit(url)
        # Fail before starting Chromium for plainly unsafe targets.
        await resolve_public(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
        directory.mkdir(parents=True, exist_ok=True)
        async with Gateway(settings) as gateway, async_playwright() as pw:
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
            launch = dict(
                headless=True,
                args=args,
                proxy={"server": gateway.address},
                chromium_sandbox=not settings.no_sandbox,
            )
            if settings.executable_path:
                launch["executable_path"] = settings.executable_path
            browser = await pw.chromium.launch(**launch)
            try:
                context = await browser.new_context(
                    viewport={"width": settings.width, "height": 900},
                    device_scale_factor=1,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    accept_downloads=False,
                    service_workers="block",
                    java_script_enabled=settings.javascript,
                )
                context.set_default_timeout(10000)
                context.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))
                await context.route_web_socket("**/*", lambda ws: ws.close())

                async def guard(route):
                    request = route.request
                    try:
                        target = normalize_url(request.url)
                        host = urlsplit(target).hostname
                        # Also enforce the allowlist at the connection gateway: browser redirect
                        # chains may reuse an earlier route decision without a second callback.
                        allowed = selectable(target, settings, explicit=True)
                        if not host:
                            allowed = False
                        if (
                            not allowed
                            or request.method not in {"GET", "HEAD"}
                            or request.resource_type in {"media", "websocket", "eventsource"}
                        ):
                            await route.abort()
                            return
                        await route.continue_()
                    except Exception:
                        await route.abort()

                await context.route("**/*", guard)
                page = await context.new_page()
                context.on("page", lambda popup: asyncio.create_task(popup.close()))
                response = await page.goto(url, wait_until="domcontentloaded")
                if response is None or response.status >= 400:
                    status = response.status if response else "无响应"
                    raise CaptureError(
                        f"目标站点返回 {status}，可能需要登录或人机验证；不会绕过站点保护。"
                    )
                content_type = response.headers.get("content-type", "").lower()
                if content_type and not any(
                    t in content_type for t in ("text/html", "application/xhtml+xml", "text/plain")
                ):
                    raise CaptureError("该链接不是可截图的网页。")
                await page.wait_for_timeout(settings.settle_ms)
                title = (await page.title()).lower()
                if any(
                    t in title
                    for t in (
                        "just a moment",
                        "attention required",
                        "checking your browser",
                        "人机验证",
                    )
                ):
                    raise CaptureError(
                        "目标站点要求人机验证；请更换可用出口，插件不会自动绕过验证。"
                    )
                note = "网页快照"
                if nodeseek_url(url):
                    script = Path(__file__).with_name("nodeseek.js").read_text(encoding="utf-8")
                    data = await page.evaluate(script, settings.max_comments)
                    if data.get("error"):
                        raise CaptureError(
                            "未找到 NodeSeek 帖子正文；页面可能需要登录、验证或已改版。"
                        )
                    html = nodeseek_html(data, url)
                    if len(html) > 1500000:
                        raise CaptureError("帖子内容过大，已停止截图。")
                    # Drop the original page scripts and build a sanitized content-only card.
                    await page.goto("about:blank")
                    await page.set_content(html, wait_until="domcontentloaded")
                    note = "NodeSeek 正文 + 置顶 / Hot 评论（仅第一页可见内容）"
                await self._settle_images(page)
                if gateway.exhausted:
                    raise CaptureError("网页加载流量超过 40 MiB 限制。")
                size = await page.evaluate("""() => ({
                    width: window.innerWidth,
                    height: Math.max(document.body?.scrollHeight || 0, document.documentElement.scrollHeight)
                })""")
                height = min(settings.max_height, math.ceil(size["height"]))
                paths = []
                for y in range(0, height, settings.slice_height):
                    path = directory / f"{len(paths) + 1:02d}.png"
                    await page.screenshot(
                        path=str(path),
                        full_page=True,  # clip uses document, not viewport coordinates for later slices.
                        clip={
                            "x": 0,
                            "y": y,
                            "width": settings.width,
                            "height": min(settings.slice_height, height - y),
                        },
                        animations="disabled",
                        timeout=10000,
                    )
                    paths.append(path)
                if size["height"] > height:
                    note += f"；超过 {height}px，底部已截断"
                if gateway.blocked:
                    note += "；部分资源因访问策略被阻止"
                return Snapshot(paths, note)
            finally:
                await browser.close()

    async def _settle_images(self, page):
        # Trigger nearby lazy images without endlessly scrolling an infinite feed.
        await page.evaluate(
            """async (limit) => {
            for (const img of Array.from(document.images).slice(0, 60)) img.loading = 'eager';
            const max = Math.min(document.documentElement.scrollHeight, limit);
            for (let y = 0; y < max; y += 800) window.scrollTo(0, y);
            window.scrollTo(0, 0);
            await Promise.race([
                Promise.all(Array.from(document.images).slice(0, 60).map(img =>
                    img.complete ? Promise.resolve() : new Promise(resolve => {
                        img.addEventListener('load', resolve, {once:true});
                        img.addEventListener('error', resolve, {once:true});
                    }))), new Promise(resolve => setTimeout(resolve, 2500))
            ]);
            if (document.fonts) await Promise.race([document.fonts.ready, new Promise(r => setTimeout(r, 1000))]);
        }""",
            self.settings.max_height,
        )
