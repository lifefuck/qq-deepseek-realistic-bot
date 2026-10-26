# -*- coding: utf-8 -*-
"""每条消息一个短 ID，日志里全程带着走。

排查「刚才那句怎么没回」时，按时间戳翻日志要人命。有了 rid，同一条消息
从收到、触发判定、调模型到发送的每一行都能串起来。

asyncio 下每条消息一个独立 Task，contextvar 天然隔离，不会串台。
"""

from contextvars import ContextVar
import logging
import secrets

_rid: ContextVar[str] = ContextVar("rid", default="-")
_installed = False


def begin(prefix: str) -> str:
    """给当前消息开一个新的 rid，形如 g<群号>-<QQ>-9f3a。"""
    rid = f"{prefix}-{secrets.token_hex(2)}"
    _rid.set(rid)
    return rid


def current() -> str:
    return _rid.get()


def install() -> None:
    """给所有日志记录兜底补 rid。

    用 record factory 而不是 handler filter：以后谁再加 handler、换 formatter，
    只要格式串里有 %(rid)s 就一定有值，不会因为少挂一个 filter 就刷屏 KeyError。
    """
    global _installed
    if _installed:
        return
    _installed = True

    previous_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous_factory(*args, **kwargs)
        if not hasattr(record, "rid"):
            record.rid = _rid.get()
        return record

    logging.setLogRecordFactory(factory)
