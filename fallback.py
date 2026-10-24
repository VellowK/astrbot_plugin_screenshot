"""Conservative parser cooperation and bounded, monotonic rate/dedup state."""

import time
from urllib.parse import urlsplit

from .config import Settings
from .policy import capture_url, extract_urls, selectable

WHOLE_FLAGS = ("screenshot_handled", "url_parser_handled", "link_parser_handled")
URL_FLAGS = ("handled_urls", "parsed_urls", "screenshot_handled_urls")


def event_handled(event) -> bool:
    # _has_send_oper is an AstrBot implementation detail, so use it only conservatively.
    if event.is_stopped() or getattr(event, "_has_send_oper", False):
        return True
    if any(event.get_extra(key, False) for key in WHOLE_FLAGS):
        return True
    result = event.get_result()
    return bool(result and getattr(result, "chain", None))


def candidates(event, settings: Settings, *, explicit: bool = False) -> list[str]:
    if not explicit and event_handled(event):
        return []
    handled = set()
    if not explicit:
        for key in URL_FLAGS:
            values = event.get_extra(key, [])
            if isinstance(values, (list, tuple, set)):
                for value in values:
                    if isinstance(value, str):
                        for url in extract_urls(value):
                            handled.add(capture_url(url))
    result = []
    for url in extract_urls(event.message_str):
        url = capture_url(url)
        if (
            url not in handled
            and url not in result
            and selectable(url, settings, explicit=explicit)
        ):
            result.append(url)
        if len(result) >= settings.max_links:
            break
    return result


def public_label(url: str) -> str:
    # Do not echo sensitive queries or tokens into a group chat or logs.
    return urlsplit(url).hostname or "网页"


class Reservations:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cooldowns: dict[tuple, float] = {}
        self.success: dict[tuple, float] = {}
        self.busy: set[tuple] = set()

    def reserve(
        self, session: str, sender: str, url: str, *, same_batch: bool = False
    ) -> tuple | None:
        now = time.monotonic()
        for store, seconds in (
            (self.cooldowns, self.settings.cooldown),
            (self.success, self.settings.dedup_seconds),
        ):
            for key, timestamp in list(store.items()):
                if now - timestamp >= seconds:
                    del store[key]
            while len(store) >= 2048:
                store.pop(next(iter(store)))
        key = (session, url)
        user = (session, sender)
        if (
            key in self.busy
            or (user in self.cooldowns and not same_batch)
            or key in self.success
            or len(self.busy) >= self.settings.concurrency * 2
        ):
            return None
        self.busy.add(key)
        self.cooldowns[user] = now
        return key

    def finish(self, key: tuple, success: bool):
        self.busy.discard(key)
        if success:
            self.success[key] = time.monotonic()
