"""Ephemeral local forward proxy with DNS/IP pinning and domain-based upstream routing.

Even when using an upstream, CONNECT/SOCKS targets are validated numeric IPs;
there is no second, unchecked resolution of a user-supplied hostname.
"""

import asyncio
from contextlib import suppress
from urllib.parse import urlsplit

from .config import Settings, matches
from .policy import UnsafeURL, normalize_url, resolve_public


class Gateway:
    MAX_BYTES = 40 * 1024 * 1024
    MAX_CONNECTIONS = 64

    def __init__(self, settings: Settings):
        self.settings = settings
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.bytes_received = 0
        self.blocked = 0
        self.exhausted = False
        self.address = ""
        self._dns: dict[tuple[str, int], list[str]] = {}

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=16384)
        port = self.server.sockets[0].getsockname()[1]
        self.address = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *_):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        if len(self.tasks) >= self.MAX_CONNECTIONS or self.exhausted:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _connect(self, host: str, port: int):
        if self.settings.allowed_domains and not any(
            matches(host, rule) for rule in self.settings.allowed_domains
        ):
            raise UnsafeURL("域名不在访问白名单中。")
        upstream = self.settings.route(host)
        if upstream is None:
            raise UnsafeURL("域名路由已禁止该目标。")
        key = (host, port)
        if key not in self._dns:
            if len(self._dns) >= 128:
                raise UnsafeURL("网页引用的域名过多。")
            self._dns[key] = await resolve_public(host, port)
        last_error = None
        for address in self._dns[key]:
            try:
                if upstream:
                    from python_socks.async_.asyncio import Proxy

                    proxy = Proxy.from_url(upstream)
                    sock = await proxy.connect(dest_host=address, dest_port=port, timeout=8)
                    try:
                        streams = await asyncio.open_connection(sock=sock)
                    except BaseException:
                        sock.close()
                        raise
                else:
                    streams = await asyncio.wait_for(asyncio.open_connection(address, port), 8)
                return streams
            except (OSError, TimeoutError) as e:
                last_error = e
        raise OSError("目标或代理连接失败。") from last_error

    async def _pipe(self, reader, writer, *, inbound: bool):
        while True:
            data = await reader.read(65536)
            if not data:
                return
            if inbound:
                self.bytes_received += len(data)
                if self.bytes_received > self.MAX_BYTES:
                    self.exhausted = True
                    for active in list(self.writers):
                        active.close()
                    raise OSError("网页流量超过限制。")
            writer.write(data)
            await writer.drain()

    async def _handle(self, client_reader, client_writer):
        self.writers.add(client_writer)
        remote_writer = None
        tunnel_started = False
        pumps = []
        try:
            header = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), 10)
            lines = header.decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ")
            if version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise UnsafeURL("HTTP 版本不支持。")
            connect = method == "CONNECT"
            if not connect and method not in {"GET", "HEAD"}:
                raise UnsafeURL("仅允许只读请求。")
            url = normalize_url(("https://" + target + "/") if connect else target)
            parsed = urlsplit(url)
            if not connect and parsed.scheme != "http":
                raise UnsafeURL("HTTPS 必须使用 CONNECT。")
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            remote_reader, remote_writer = await self._connect(host, port)
            self.writers.add(remote_writer)
            if connect:
                client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await client_writer.drain()
                tunnel_started = True
            else:
                path = parsed.path + (("?" + parsed.query) if parsed.query else "")
                forwarded = [f"{method} {path} HTTP/1.1", f"Host: {parsed.netloc}"]
                for line in lines[1:]:
                    if not line:
                        continue
                    name, _, _value = line.partition(":")
                    if name.lower() not in {
                        "host",
                        "connection",
                        "proxy-connection",
                        "proxy-authorization",
                        "content-length",
                        "transfer-encoding",
                        "upgrade",
                        "expect",
                    }:
                        forwarded.append(line)
                forwarded.extend(["Connection: close", "", ""])
                remote_writer.write("\r\n".join(forwarded).encode("iso-8859-1"))
                await remote_writer.drain()
                tunnel_started = True
            # A half-closed or cancelled tunnel must not leave its other pump running.
            pumps = [
                asyncio.create_task(self._pipe(client_reader, remote_writer, inbound=False)),
                asyncio.create_task(self._pipe(remote_reader, client_writer, inbound=True)),
            ]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except UnsafeURL:
            self.blocked += 1
            if not tunnel_started:
                with suppress(Exception):
                    client_writer.write(
                        b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                    )
                    await client_writer.drain()
        except (Exception,):
            # Never expose proxy credentials, URL queries, DNS answers, or page content.
            if not tunnel_started:
                with suppress(Exception):
                    client_writer.write(
                        b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                    )
                    await client_writer.drain()
        finally:
            for pump in pumps:
                if not pump.done():
                    pump.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            for writer in (remote_writer, client_writer):
                if writer:
                    self.writers.discard(writer)
                    writer.close()
                    with suppress(Exception):
                        await writer.wait_closed()
