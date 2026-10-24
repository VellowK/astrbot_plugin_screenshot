"""URL selection and DNS validation; the gateway connects to validated numeric IPs."""

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

from .config import Settings, matches


class UnsafeURL(ValueError):
    pass


URL_RE = re.compile(r"https?://[^\s<>\x00-\x1f\x7f\u3000，。！？；、（）【】《》「」]+", re.I)
TRANSITION_NETS = tuple(
    ipaddress.ip_network(x)
    for x in (
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2001::/32",
        "2002::/16",
    )
)


def normalize_url(value: str) -> str:
    if len(value) > 4096 or re.search(r"[\x00-\x20\x7f\\]", value):
        raise UnsafeURL("链接无效或过长。")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        port = parsed.port
        if port not in {None, 80, 443}:
            raise ValueError
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
        if not host or "%" in host or any(c in host for c in "/\\@?# \t\r\n"):
            raise ValueError
        if ":" in host:
            ipaddress.IPv6Address(host)
            host = "[" + host + "]"
        else:
            if not re.fullmatch(r"[a-z0-9.-]+", host):
                raise ValueError
        authority = host + (f":{port}" if port is not None else "")
        return urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError) as e:
        raise UnsafeURL("只支持无账号密码的 HTTP(S) 链接，端口限 80/443。") from e


def extract_urls(text: str) -> list[str]:
    result = []
    for match in URL_RE.finditer(text):
        raw = match.group().rstrip(".,;!?'\"`]}")
        while raw.endswith(")") and raw.count(")") > raw.count("("):
            raw = raw[:-1]
        try:
            url = normalize_url(raw)
        except UnsafeURL:
            continue
        if url not in result:
            result.append(url)
        if len(result) >= 20:
            break
    return result


def nodeseek_url(url: str) -> bool:
    p = urlsplit(url)
    return p.hostname in {"nodeseek.com", "www.nodeseek.com"} and bool(
        re.fullmatch(r"/post-\d+(?:-\d+)?/?", p.path)
    )


def capture_url(url: str) -> str:
    if nodeseek_url(url):
        p = urlsplit(url)
        post_id = re.match(r"/post-(\d+)", p.path).group(1)
        return urlunsplit((p.scheme, p.netloc, f"/post-{post_id}-1", "", ""))
    return url


def selectable(url: str, settings: Settings, *, explicit: bool = False) -> bool:
    host = urlsplit(url).hostname or ""
    if settings.allowed_domains and not any(matches(host, d) for d in settings.allowed_domains):
        return False
    if settings.route(host) is None:
        return False
    return explicit or not any(matches(host, d) for d in settings.skip_domains)


def public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    if not ip.is_global or ip.is_multicast or ip.is_unspecified:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return public_ip(str(ip.ipv4_mapped))
        if any(ip in network for network in TRANSITION_NETS):
            return False
    return True


async def resolve_public(host: str, port: int) -> list[str]:
    if port not in {80, 443}:
        raise UnsafeURL("不允许访问该端口。")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            records = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
                8,
            )
        except (OSError, TimeoutError) as e:
            raise UnsafeURL("域名解析失败，请检查服务器 DNS 或代理配置。") from e
        addresses = list(dict.fromkeys(record[4][0] for record in records))
    # Reject mixed public/private DNS, not merely the chosen address.
    if not addresses or not all(public_ip(ip) for ip in addresses):
        raise UnsafeURL("已阻止内网、回环、保留地址或混合 DNS 目标。")
    return addresses
