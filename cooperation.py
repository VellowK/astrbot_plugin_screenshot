"""Event-local response observation; never patch the shared platform client.

Some parsers restore AstrBot's _has_send_oper after yielding their response.
Remember successful sends independently, including OneBot merged forwards. A
request-local client facade covers direct calls without affecting other events.
"""

from contextvars import ContextVar
from functools import wraps

RESPONSE_SEEN = "screenshot_other_response_seen"
_OBSERVING = "screenshot_response_observer"
_OWN_SEND = ContextVar("screenshot_own_send", default=None)
_ONEBOT_SENDS = frozenset(
    {
        "send_msg",
        "send_group_msg",
        "send_private_msg",
        "send_forward_msg",
        "send_group_forward_msg",
        "send_private_forward_msg",
        "upload_group_file",
        "upload_private_file",
    }
)


def response_seen(event) -> bool:
    return bool(event.get_extra(RESPONSE_SEEN, False))


def remember_response(event) -> None:
    if _OWN_SEND.get() is not event:
        event.set_extra(RESPONSE_SEEN, True)


def _succeeded(result) -> bool:
    # OneBot normally raises on failure; also accept clients returning envelopes.
    return not (
        isinstance(result, dict)
        and (result.get("status") == "failed" or result.get("retcode", 0) not in (0, "0", None))
    )


class _EventBot:
    """Delegate everything except observing completed OneBot message actions."""

    def __init__(self, bot, event):
        self._bot = bot
        self._event = event

    def __getattr__(self, name):
        original = getattr(self._bot, name)
        if not callable(original) or name not in {"call_action", "call_api"} | _ONEBOT_SENDS:
            return original

        @wraps(original)
        async def observed(*args, **kwargs):
            action = (
                (args[0] if args else kwargs.get("action"))
                if name in {"call_action", "call_api"}
                else name
            )
            result = await original(*args, **kwargs)
            if action in _ONEBOT_SENDS and _succeeded(result):
                remember_response(self._event)
            return result

        return observed


def observe_responses(event) -> None:
    """Install once, before ordinary parsers. State belongs to this input only."""
    if event.get_extra(_OBSERVING, False):
        return
    original_send = event.send

    @wraps(original_send)
    async def observed_send(message, *args, **kwargs):
        result = await original_send(message, *args, **kwargs)
        if getattr(message, "chain", message):
            remember_response(event)
        return result

    event.send = observed_send
    bot = getattr(event, "bot", None)
    if bot is not None and any(
        callable(getattr(bot, name, None)) for name in ("call_action", "call_api")
    ):
        event.bot = _EventBot(bot, event)
    event.set_extra(_OBSERVING, True)


async def send_screenshot(event, message):
    """Exclude our own response, not concurrent sends by another plugin task."""
    token = _OWN_SEND.set(event)
    try:
        return await event.send(message)
    finally:
        _OWN_SEND.reset(token)
