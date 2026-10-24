"""Validated, immutable settings. No credentials are included in error messages."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


def domain(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    if value.startswith("*."):
        return "*." + domain(value[2:])
    if not value or any(c in value for c in "/:@?#\\ \t\r\n*"):
        raise ConfigError("域名规则格式错误；请填写域名或 *.example.com，不含协议/端口。")
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise ConfigError("域名规则无法识别。") from e


def matches(host: str, pattern: str) -> bool:
    """A bare domain includes its subdomains; *.domain excludes the apex."""
    if pattern.startswith("*."):
        return host.endswith("." + pattern[2:])
    return host == pattern or host.endswith("." + pattern)


def proxy_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "socks5"}
            or not parsed.hostname
            or parsed.port is None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        _ = parsed.username, parsed.password
    except ValueError as e:
        raise ConfigError(
            "代理格式应为 http://[user:pass@]host:port 或 socks5://[user:pass@]host:port。"
        ) from e
    return value


@dataclass(frozen=True)
class RouteRule:
    pattern: str
    action: str
    proxy: str = ""


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    auto_enabled: bool = True
    auto_install_browser: bool = True
    browser_install_timeout: int = 600
    browser_install_retries: int = 1
    browser_install_proxy: str = ""
    browser_idle_seconds: int = 120
    require_at: bool = False
    report_auto_errors: bool = False
    proxy: str = ""
    routes: tuple[RouteRule, ...] = ()
    skip_domains: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    allowed_sessions: tuple[str, ...] = ()
    max_links: int = 1
    concurrency: int = 1
    timeout: int = 45
    settle_ms: int = 1500
    width: int = 1080
    max_height: int = 12000
    slice_height: int = 3000
    cooldown: int = 15
    dedup_seconds: int = 180
    max_comments: int = 10
    javascript: bool = True
    no_sandbox: bool = False
    executable_path: str = ""

    @classmethod
    def load(cls, config: Mapping[str, Any]) -> "Settings":
        defaults = cls()
        result: dict[str, Any] = {}
        for name in (
            "enabled",
            "auto_enabled",
            "auto_install_browser",
            "require_at",
            "report_auto_errors",
            "javascript",
            "no_sandbox",
        ):
            value = config.get(name, getattr(defaults, name))
            if not isinstance(value, bool):
                raise ConfigError(f"{name} 必须为布尔值。")
            result[name] = value
        limits = {
            "browser_install_timeout": (60, 1800),
            "browser_install_retries": (0, 3),
            "browser_idle_seconds": (0, 3600),
            "max_links": (1, 3),
            "concurrency": (1, 2),
            "timeout": (10, 90),
            "settle_ms": (0, 5000),
            "width": (720, 1440),
            "max_height": (900, 16000),
            "slice_height": (900, 4000),
            "cooldown": (0, 300),
            "dedup_seconds": (0, 3600),
            "max_comments": (0, 20),
        }
        for name, (low, high) in limits.items():
            value = config.get(name, getattr(defaults, name))
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ConfigError(f"{name} 必须为 {low}～{high} 的整数。")
            result[name] = value
        for name in ("skip_domains", "allowed_domains", "allowed_sessions"):
            value = config.get(name, [])
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ConfigError(f"{name} 必须为字符串列表。")
            result[name] = tuple(domain(x) if name != "allowed_sessions" else x for x in value)
        value = config.get("proxy", "")
        if not isinstance(value, str):
            raise ConfigError("proxy 必须为字符串。")
        result["proxy"] = proxy_url(value.strip())
        value = config.get("browser_install_proxy", "")
        if not isinstance(value, str):
            raise ConfigError("browser_install_proxy 必须为字符串。")
        value = value.strip()
        if value:
            try:
                parsed = urlsplit(value)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.port is None
                    or parsed.path not in {"", "/"}
                    or parsed.query
                    or parsed.fragment
                    or any(c.isspace() for c in value)
                ):
                    raise ValueError
            except ValueError as exc:
                raise ConfigError(
                    "browser_install_proxy 应为 http(s)://[user:pass@]host:port，不支持 SOCKS。"
                ) from exc
        result["browser_install_proxy"] = value
        raw_routes = config.get("routes_json", "[]")
        try:
            routes = json.loads(raw_routes)
            if not isinstance(routes, list) or len(routes) > 100:
                raise ValueError
            rules = []
            for rule in routes:
                if not isinstance(rule, dict) or set(rule) - {"domain", "action", "proxy"}:
                    raise ValueError
                pattern = domain(rule["domain"])
                action = rule["action"]
                if action not in {"direct", "proxy", "block"}:
                    raise ValueError
                upstream = proxy_url(rule.get("proxy", "").strip())
                if action == "proxy" and not (upstream or result["proxy"]):
                    raise ValueError
                rules.append(RouteRule(pattern, action, upstream))
            result["routes"] = tuple(rules)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            raise ConfigError(
                'routes_json 无效；需要 [{"domain":"nodeseek.com","action":"proxy"}]，proxy 路由需要配置代理。'
            ) from e
        value = config.get("executable_path", "")
        if not isinstance(value, str):
            raise ConfigError("executable_path 必须为字符串。")
        result["executable_path"] = value.strip()
        return cls(**result)

    def route(self, host: str) -> str | None:
        """Return upstream URL, empty string for direct, None for blocked. First match wins."""
        for rule in self.routes:
            if matches(host, rule.pattern):
                if rule.action == "block":
                    return None
                return "" if rule.action == "direct" else (rule.proxy or self.proxy)
        return self.proxy
