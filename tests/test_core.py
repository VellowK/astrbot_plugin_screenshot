import asyncio
import socket
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_screenshot.config import ConfigError, Settings, matches
from astrbot_plugin_screenshot.fallback import Reservations, candidates, event_handled
from astrbot_plugin_screenshot.gateway import Gateway
from astrbot_plugin_screenshot.policy import (
    UnsafeURL,
    capture_url,
    extract_urls,
    normalize_url,
    public_ip,
    resolve_public,
    selectable,
)


class Event:
    message_str = "https://example.com/ https://www.nodeseek.com/post-123-9#2"
    _has_send_oper = False

    def __init__(self):
        self.extra = {}
        self.stopped = False
        self.result = None

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def is_stopped(self):
        return self.stopped

    def get_result(self):
        return self.result


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(Settings.load({}), Settings())

    def test_domain_matching(self):
        self.assertTrue(matches("www.nodeseek.com", "nodeseek.com"))
        self.assertFalse(matches("evilnodeseek.com", "nodeseek.com"))
        self.assertFalse(matches("nodeseek.com", "*.nodeseek.com"))

    def test_invalid(self):
        for config in (
            {"concurrency": 10},
            {"timeout": True},
            {"enabled": "true"},
            {"routes_json": "{}"},
            {"proxy": "https://host:123"},
            {"routes_json": '[{"domain":"example.com","action":"proxy"}]'},
            {"allowed_domains": ["https://example.com"]},
        ):
            with self.subTest(config=config), self.assertRaises(ConfigError):
                Settings.load(config)

    def test_routing(self):
        settings = Settings.load(
            {
                "proxy": "socks5://user:pass@127.0.0.1:1080",
                "routes_json": '[{"domain":"blocked.example","action":"block"},'
                '{"domain":"cn.example","action":"direct"},'
                '{"domain":"nodeseek.com","action":"proxy","proxy":"http://127.0.0.1:7890"}]',
            }
        )
        self.assertIsNone(settings.route("blocked.example"))
        self.assertEqual(settings.route("www.cn.example"), "")
        self.assertEqual(settings.route("www.nodeseek.com"), "http://127.0.0.1:7890")
        self.assertEqual(settings.route("other.example"), settings.proxy)


class PolicyTests(unittest.TestCase):
    def test_extract(self):
        self.assertEqual(
            extract_urls("链接（https://example.com/a(b)）。https://example.com/a(b)"),
            ["https://example.com/a(b)"],
        )
        self.assertEqual(
            extract_urls("https://example.com/?a=1&b=2 https://x.example/#foo"),
            ["https://example.com/?a=1&b=2", "https://x.example/"],
        )

    def test_normalize(self):
        self.assertEqual(normalize_url("HTTPS://EXAMPLE.COM.#foo"), "https://example.com/")
        self.assertEqual(
            normalize_url("https://[2606:4700:4700::1111]/"), "https://[2606:4700:4700::1111]/"
        )
        for value in (
            "file:///etc/passwd",
            "https://user:pass@example.com/",
            "http://example.com:8080",
            "http://a\\@127.0.0.1/",
            "https://a.com/\nx",
            "http://[fe80::1%25eth0]/",
        ):
            with self.subTest(value=value), self.assertRaises(UnsafeURL):
                normalize_url(value)

    def test_node_seek_first_page(self):
        self.assertEqual(
            capture_url("https://www.nodeseek.com/post-123-9?x=1#3"),
            "https://www.nodeseek.com/post-123-1",
        )
        url = "https://evilnodeseek.com/post-123-9"
        self.assertEqual(capture_url(url), url)

    def test_ips(self):
        for value in (
            "127.0.0.1",
            "0.0.0.0",
            "10.0.0.1",
            "169.254.169.254",
            "192.168.1.1",
            "100.64.0.1",
            "224.0.0.1",
            "::1",
            "fc00::1",
            "fe80::1",
            "::ffff:127.0.0.1",
            "64:ff9b::7f00:1",
            "2002:7f00:1::",
        ):
            with self.subTest(value=value):
                self.assertFalse(public_ip(value))
        self.assertTrue(public_ip("1.1.1.1"))
        self.assertTrue(public_ip("2606:4700:4700::1111"))

    def test_selection(self):
        settings = replace(Settings(), skip_domains=("example.com",))
        self.assertFalse(selectable("https://example.com/", settings))
        self.assertTrue(selectable("https://example.com/", settings, explicit=True))
        settings = replace(settings, allowed_domains=("nodeseek.com",))
        self.assertFalse(selectable("https://example.com/", settings, explicit=True))


class AsyncPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_dns(self):
        records = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=records)):
            with self.assertRaises(UnsafeURL):
                await resolve_public("mixed.example", 443)

    async def test_literal(self):
        with self.assertRaises(UnsafeURL):
            await resolve_public("127.0.0.1", 80)
        self.assertEqual(await resolve_public("1.1.1.1", 80), ["1.1.1.1"])


class CooperationTests(unittest.TestCase):
    def test_suppression(self):
        for flag in ("screenshot_handled", "url_parser_handled", "link_parser_handled"):
            event = Event()
            event.extra[flag] = True
            self.assertEqual(candidates(event, Settings()), [])
        event = Event()
        event._has_send_oper = True
        self.assertTrue(event_handled(event))
        self.assertEqual(len(candidates(event, Settings(), explicit=True)), 1)
        event = Event()
        event.stopped = True
        self.assertEqual(candidates(event, Settings()), [])

    def test_partial_and_canonical(self):
        event = Event()
        event.extra["parsed_urls"] = ["https://example.com/"]
        self.assertEqual(candidates(event, Settings()), ["https://www.nodeseek.com/post-123-1"])
        event.extra["handled_urls"] = ["https://www.nodeseek.com/post-123-2"]
        self.assertEqual(candidates(event, Settings()), [])

    def test_rate_dedup_and_failure_retry(self):
        settings = replace(Settings(), cooldown=0)
        reservations = Reservations(settings)
        key = reservations.reserve("s", "u", "https://example.com/")
        self.assertIsNotNone(key)
        self.assertIsNone(reservations.reserve("s", "u2", key[1]))
        reservations.finish(key, False)
        key = reservations.reserve("s", "u", key[1])
        self.assertIsNotNone(key)
        reservations.finish(key, True)
        self.assertIsNone(reservations.reserve("s", "u2", key[1]))
        self.assertIsNotNone(reservations.reserve("other", "u", key[1]))

    def test_batch_cooldown(self):
        reservations = Reservations(Settings())
        a = reservations.reserve("s", "u", "https://a.example/")
        reservations.finish(a, True)
        self.assertIsNone(reservations.reserve("s", "u", "https://b.example/"))
        self.assertIsNotNone(reservations.reserve("s", "u", "https://b.example/", same_batch=True))


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, gateway, header):
        port = int(gateway.address.rsplit(":", 1)[1])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(header)
        await writer.drain()
        result = await asyncio.wait_for(reader.read(4096), 3)
        writer.close()
        await writer.wait_closed()
        return result

    async def test_blocks_private_and_methods(self):
        async with Gateway(Settings()) as gateway:
            for header in (
                b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n",
                b"GET http://169.254.169.254/ HTTP/1.1\r\n\r\n",
                b"POST http://example.com/ HTTP/1.1\r\n\r\n",
                b"CONNECT example.com:22 HTTP/1.1\r\n\r\n",
            ):
                self.assertIn(b"403 Forbidden", await self.request(gateway, header))

    async def test_domain_allowlist_covers_every_connection(self):
        gateway = Gateway(replace(Settings(), allowed_domains=("example.com",)))
        with patch("astrbot_plugin_screenshot.gateway.resolve_public", AsyncMock()) as dns:
            for host in ("other.example", "example.com.evil.example"):
                with self.subTest(host=host), self.assertRaises(UnsafeURL):
                    await gateway._connect(host, 443)
            dns.assert_not_awaited()

    async def test_byte_limit(self):
        gateway = Gateway(Settings())
        gateway.MAX_BYTES = 3
        reader = asyncio.StreamReader()
        reader.feed_data(b"four")
        reader.feed_eof()
        writer = MagicMock(drain=AsyncMock())
        gateway.writers.add(writer)
        with self.assertRaises(OSError):
            await gateway._pipe(reader, writer, inbound=True)
        self.assertTrue(gateway.exhausted)
        writer.close.assert_called_once()
        writer.write.assert_not_called()

    async def test_direct_pin_and_cache(self):
        gateway = Gateway(Settings())
        streams = (object(), object())
        with (
            patch(
                "astrbot_plugin_screenshot.gateway.resolve_public",
                AsyncMock(return_value=["1.1.1.1"]),
            ) as dns,
            patch(
                "astrbot_plugin_screenshot.gateway.asyncio.open_connection",
                AsyncMock(return_value=streams),
            ) as conn,
        ):
            self.assertEqual(await gateway._connect("example.com", 443), streams)
            await gateway._connect("example.com", 443)
            self.assertEqual(dns.await_count, 1)
            conn.assert_awaited_with("1.1.1.1", 443)

    async def test_upstream_receives_ip(self):
        from python_socks.async_.asyncio import Proxy

        gateway = Gateway(replace(Settings(), proxy="socks5://user:secret@127.0.0.1:1080"))
        proxy = type("ProxyMock", (), {"connect": AsyncMock(return_value=object())})()
        with (
            patch(
                "astrbot_plugin_screenshot.gateway.resolve_public",
                AsyncMock(return_value=["1.1.1.1"]),
            ),
            patch.object(Proxy, "from_url", return_value=proxy),
            patch(
                "astrbot_plugin_screenshot.gateway.asyncio.open_connection",
                AsyncMock(return_value=(1, 2)),
            ),
        ):
            await gateway._connect("www.nodeseek.com", 443)
            proxy.connect.assert_awaited_once_with(dest_host="1.1.1.1", dest_port=443, timeout=8)


if __name__ == "__main__":
    unittest.main()
